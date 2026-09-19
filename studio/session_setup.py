"""Prepare and qualify suites only after an explicit, durable setup request.

The controller owns this state machine. Offline preparation holds the same lock
as updates and launches; live qualification uses the ordinary durable queue.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from common import atomic_json, now, read_json
from . import config, db, session_control
from .session_catalog import (
    ROOT,
    PROTOCOL_VERSION,
    digest_tree,
    qualification_state,
    readiness,
    receipt,
)

SUITES = ("coding-sessions", "vision-checks", "visual-design")
PAUSED_DETAIL = "Suite setup is idle. Select Start setup to validate fixtures and run model smoke tests. Updates and restarts do not start checks. Existing benchmark profiles remain available."


def controls(state):
    control = db.state(session_control.KEY, {})
    active = bool(control.get("active") and control.get("revision") == config.REVISION)
    state.update(can_start=not active, can_stop=active)
    if not active and (state.get("phase") == "preparing" or session_control.stopping()):
        state.update(
            phase="stopping",
            detail="Stopping suite setup and its benchmarks. Evidence is retained; updates can proceed after cleanup finishes.",
            can_start=False,
        )
    elif active and state.get("request_id") != control["id"]:
        state.update(
            phase="requested",
            detail="Setup requested. Waiting for the controller; no new setup request is needed.",
            suites={},
        )
    elif not active and state.get("phase") != "ready":
        detail = (
            "Last setup attempt failed: " + state["last_error"] + ". "
            if state.get("last_error")
            else ""
        ) + PAUSED_DETAIL
        state.update(phase="paused", detail=detail)
    return state


def qualification_summary(suites):
    phases = {s["phase"] for s in suites.values()}
    if phases == {"ready"}:
        return "ready", "Benchmark suites are ready."
    count = sum(s["phase"] == "ready" for s in suites.values())
    prefix = f"Offline preparation passed. {count} of {len(suites)} new suites ready. "
    suffix = " Each suite unlocks after a passing smoke run. Existing benchmark profiles remain available."
    if phases & {
        "failed",
        "interrupted",
        "invalid",
        "cancelled",
        "blocked",
        "not_passed",
        "missing_run",
    }:
        return (
            "needs_attention",
            prefix
            + "Some smoke runs need attention; open their results for details and retry when resolved."
            + suffix,
        )
    if phases & (db.ACTIVE | db.WAITING):
        return "qualifying", prefix + "Live smoke tests run one at a time." + suffix
    return (
        "waiting",
        prefix + "Smoke tests are queued or waiting for an available model." + suffix,
    )


def status():
    """Present current qualification progress without scheduling or inference."""
    path = config.DATA / "session-setup.json"
    if not path.exists():
        return controls({"phase": "paused", "detail": PAUSED_DETAIL, "suites": {}})
    state = read_json(path)
    if state.get("phase") not in {
        "ready",
        "qualifying",
        "waiting",
        "needs_attention",
        "paused",
        "requested",
    }:
        return controls(state)
    evidence = receipt()
    suites = {}
    for suite, saved in state.get("suites", {}).items():
        readiness_state = readiness(suite, evidence=evidence, setup=state)
        if not readiness_state["prepared"]:
            state.update(
                phase="waiting_for_preparation",
                detail="The preparation receipt no longer matches this deployment. Waiting for the controller to validate fixtures before qualification.",
                suites={},
            )
            return controls(state)
        if readiness_state["ready"]:
            suites[suite] = {
                "phase": "ready",
                "run_id": evidence["suites"][suite]["qualified_run"],
            }
        else:
            suites[suite] = qualification_state(saved)
    if suites:
        phase, detail = qualification_summary(suites)
        state.update(phase=phase, detail=detail, suites=suites)
    return controls(state)


class SessionSetup:
    def __init__(self):
        self.child = None
        self.lock = None
        self.image = None
        self.owner = "session-setup-" + uuid.uuid4().hex[:16]
        self.failed = False
        self.request_id = None
        self.inactive_published = False
        self.next_check = 0
        self.state = {"revision": config.REVISION, "owner": self.owner}
        path = config.DATA / "session-setup.json"
        self.previous = read_json(path) if path.exists() else {}

    def inspect_image(self):
        if self.image is None:
            self.image = subprocess.check_output(
                [
                    "docker",
                    "image",
                    "inspect",
                    config.SESSION_IMAGE,
                    "--format",
                    "{{.Id}}",
                ],
                text=True,
                timeout=15,
            ).strip()
        self.state["expected_image_id"] = self.image

    def sync_control(self):
        """Honor Stop even while a benchmark or preparation holds the scheduler."""
        control = session_control.claim(self.owner)
        if self.previous.get("phase") == "preparing" and self.previous.get("owner"):
            self.cleanup(self.previous["owner"])
        self.previous = {}
        if not control.get("active"):
            self.stop()
            self.request_id = None
            if not self.inactive_published:
                try:
                    self.inspect_image()
                except Exception:
                    self.state["expected_image_id"] = "unavailable"
                self.publish(
                    "paused",
                    PAUSED_DETAIL,
                    request_id=None,
                    suites=self.state.get("suites")
                    or {s: {"phase": "pending"} for s in SUITES},
                )
                self.inactive_published = True
            return False
        if self.request_id != control["id"]:
            self.stop()
            self.failed = False
            self.next_check = 0
            self.image = None
            self.request_id = control["id"]
            self.state = {
                "revision": config.REVISION,
                "owner": self.owner,
                "request_id": self.request_id,
            }
            self.publish(
                "requested",
                "Setup requested. Waiting for the machine to be available.",
                suites={s: {"phase": "pending"} for s in SUITES},
            )
        self.inactive_published = False
        return True

    def publish(self, phase, detail, **values):
        self.state.update(phase=phase, detail=detail, updated_at=now(), **values)
        atomic_json(config.DATA / "session-setup.json", self.state)

    def release(self):
        if self.lock:
            self.lock.close()
            self.lock = None

    def cleanup(self, owner=None):
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=io.bench-studio.preparation=" + (owner or self.owner),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        ids = result.stdout.split()
        if ids:
            subprocess.run(
                ["docker", "rm", "-f", *ids],
                capture_output=True,
                timeout=45,
                check=True,
            )

    def stop(self):
        if self.child:
            if self.child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.child.pid, signal.SIGTERM)
                try:
                    self.child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.child.pid, signal.SIGKILL)
                    self.child.wait(timeout=5)
            try:
                self.cleanup()
            finally:
                self.child = None
                self.release()
                self.publish(
                    "interrupted",
                    "Fixture preparation stopped; evidence retained. Select Start setup to try again.",
                )

    def tick(self):
        """Return True while offline checks reserve the machine."""
        if not self.sync_control():
            return False
        if self.failed:
            return False
        try:
            return self.advance()
        except Exception as exc:
            self.failed = True
            try:
                self.stop()
            finally:
                self.release()
                self.publish(
                    "failed", "Suite setup failed: " + str(exc), last_error=str(exc)
                )
                session_control.finish(self.request_id, "failed")
            return False

    def advance(self):
        finished_preparation = False
        if self.child:
            result = self.child.poll()
            if result is None:
                return True
            self.cleanup()
            self.child = None
            self.release()
            if result:
                raise RuntimeError(
                    "offline fixture checks failed; see " + self.state["log"]
                )
            finished_preparation = True
            self.next_check = 0
        if time.monotonic() < self.next_check:
            return False
        self.next_check = time.monotonic() + 15
        self.inspect_image()
        evidence = receipt()
        prepared = (
            evidence.get("image_id") == self.image
            and evidence.get("source_hash") == digest_tree(ROOT)
            and evidence.get("protocol_version") == PROTOCOL_VERSION
            and all(
                evidence.get("suites", {}).get(suite, {}).get("passed")
                for suite in SUITES
            )
        )
        if not prepared:
            if finished_preparation:
                raise RuntimeError(
                    "Preparation exited without a matching successful receipt"
                )
            self.lock = (config.DATA / ".execution.lock").open("a")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.release()
                return False
            if (config.DATA / ".maintenance").exists() or any(
                m["status"] in db.ACTIVE | db.WAITING for m in db.runs()
            ):
                self.release()
                return False
            directory = config.DATA / "session-validation" / self.owner
            directory.mkdir(parents=True, exist_ok=True)
            log = directory / "preparation.log"
            with log.open("a") as output:
                self.child = subprocess.Popen(
                    [
                        sys.executable,
                        str(config.ROOT / "scripts/prepare-sessions.py"),
                        "--no-build",
                        "--image",
                        self.image,
                        "--suite",
                        "all",
                    ],
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=dict(
                        os.environ,
                        STUDIO_PREPARATION_OWNER=self.owner,
                        STUDIO_CONTROLLER_PID=str(os.getpid()),
                    ),
                )
            self.publish(
                "preparing",
                "Preparing the new benchmark suites on this machine. Offline reference and negative-control checks are running.",
                log=str(log),
            )
            return True
        return self.qualify(evidence)

    def qualify(self, evidence):
        from . import discovery
        from .api import Launch, launch

        suites = {}
        for suite in SUITES:
            if readiness(suite)["ready"]:
                suites[suite] = {
                    "phase": "ready",
                    "run_id": evidence["suites"][suite].get("qualified_run"),
                }
                continue
            matches = [
                m
                for m in db.runs()
                if (
                    m.get("profile_spec", {}).get("qualification")
                    and m.get("setup_request_id") == self.request_id
                    and m["profile_spec"].get("suite") == suite
                    and m["profile_spec"].get("fixture_source_hash")
                    == evidence["source_hash"]
                    and m["profile_spec"].get("session_image") == self.image
                    and m.get("revision") == config.REVISION
                )
            ]
            if matches:
                run = matches[0]
                suites[suite] = qualification_state({"run_id": run["id"]})
                continue
            try:
                models = discovery.discover()["models"]
            except Exception as exc:
                suites[suite] = {
                    "phase": "waiting_for_model",
                    "detail": "Runtime discovery unavailable: " + str(exc),
                }
                continue
            model = next(
                (
                    m
                    for m in models
                    if m["alias"] == config.SESSION_SMOKE_TARGET and m["available"]
                ),
                None,
            )
            if not model or (
                suite != "coding-sessions" and model.get("vision") is not True
            ):
                suites[suite] = {
                    "phase": "waiting_for_model",
                    "detail": "Waiting for an available "
                    + config.SESSION_SMOKE_TARGET
                    + (
                        " model with advertised vision support."
                        if suite != "coding-sessions"
                        else " model."
                    ),
                }
                continue
            key = hashlib.sha256(
                json.dumps(
                    [
                        config.REVISION,
                        suite,
                        evidence["source_hash"],
                        self.image,
                        self.request_id,
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            try:
                run = launch(
                    Launch(
                        profile=suite,
                        targets=[config.SESSION_SMOKE_TARGET],
                        task_selection="text-1"
                        if suite == "vision-checks"
                        else "issues-small",
                        repetitions=1,
                        review_mode="unattended",
                        qualification=True,
                        setup_request_id=self.request_id,
                        idempotency_key="setup-qualification-" + key,
                        note="User-requested qualification: one representative task; not a model ranking.",
                    )
                )
            except Exception as exc:
                suites[suite] = {
                    "phase": "waiting_for_model",
                    "detail": "Qualification could not be queued: " + str(exc),
                }
                continue
            suites[suite] = {"phase": "queued", "run_id": run["id"]}
        phase, detail = qualification_summary(suites)
        self.publish(phase, detail, suites=suites)
        if all(
            s["phase"]
            in db.TERMINAL | {"ready", "blocked", "not_passed", "missing_run"}
            for s in suites.values()
        ):
            session_control.finish(self.request_id, phase)
        return False
