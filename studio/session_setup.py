"""Provision new suites after deployment without delaying application health.

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
from . import config, db
from .session_catalog import ROOT, PROTOCOL_VERSION, digest_tree, readiness, receipt

SUITES = ("coding-sessions", "vision-checks", "visual-design")


class SessionSetup:
    def __init__(self):
        self.child = None
        self.lock = None
        self.image = None
        self.owner = "session-setup-" + uuid.uuid4().hex[:16]
        self.failed = False
        self.next_check = 0
        self.state = {"revision": config.REVISION, "owner": self.owner}
        path = config.DATA / "session-setup.json"
        self.previous = read_json(path) if path.exists() else {}

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
                    "Fixture preparation interrupted; evidence retained. It will retry after controller restart.",
                )

    def tick(self):
        """Return True while offline checks reserve the machine."""
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
                self.publish("failed", "Automatic suite setup failed: " + str(exc))
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
        if self.image is None:
            if self.previous.get("phase") == "preparing" and self.previous.get("owner"):
                self.cleanup(self.previous["owner"])
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
                    and m["profile_spec"].get("suite") == suite
                    and m["profile_spec"].get("fixture_source_hash")
                    == evidence["source_hash"]
                    and m["profile_spec"].get("session_image") == self.image
                    and m.get("revision") == config.REVISION
                )
            ]
            if matches:
                run = matches[0]
                suites[suite] = {
                    "phase": run["status"],
                    "run_id": run["id"],
                    "detail": run.get("error") or run.get("progress"),
                }
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
                    [config.REVISION, suite, evidence["source_hash"], self.image],
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
                        idempotency_key="automatic-qualification-" + key,
                        note="Deployment qualification: one representative task; not a model ranking.",
                    )
                )
            except Exception as exc:
                suites[suite] = {
                    "phase": "waiting_for_model",
                    "detail": "Qualification could not be queued: " + str(exc),
                }
                continue
            suites[suite] = {"phase": "queued", "run_id": run["id"]}
        complete = all(s["phase"] == "ready" for s in suites.values())
        self.publish(
            "ready" if complete else "qualifying",
            "Benchmark suites are ready."
            if complete
            else "Offline preparation passed. Live suite qualification uses the normal idle queue; suites remain gated until their smoke run passes.",
            suites=suites,
        )
        return False
