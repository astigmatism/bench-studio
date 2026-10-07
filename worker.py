"""One bounded container job, targeting one service or a sequential/parallel pair."""

from __future__ import annotations

import concurrent.futures
import fcntl
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from common import (
    HEALTH_GRACE,
    ROUTER_SWITCH_WAIT,
    TRANSIENT,
    ModelConfigurationChanged,
    ModelOffline,
    RouterSwitching,
    RuntimeUnavailable,
    atomic_json,
    check_drift,
    classified,
    client_name,
    drains_overlapping,
    error_kind,
    error_message,
    now,
    read_json,
    require_idle,
    resolve,
    router_error,
    snapshot,
    validate_results,
    wait_for_runtime,
    write_index,
)

# A phase that overlaps a router drain is excluded and measured again, at most
# this many times in total.
MAX_PHASE_ATTEMPTS = 3


def compatibility_probe(endpoint, model, client=None):
    """Small untimed compatibility request; requires real usage and clean SSE end."""
    u = urllib.parse.urlparse(endpoint)
    connection = http.client.HTTPConnection(u.hostname, u.port, timeout=120)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the word ready."}],
        "max_tokens": 64,
        "temperature": 0,
        "seed": 42,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    usage, finish, done, chunks = None, None, False, 0
    try:
        connection.request(
            "POST",
            u.path.rstrip("/") + "/chat/completions",
            json.dumps(payload),
            {"Content-Type": "application/json", "X-Client-Name": client or client_name()},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise router_error(response.status, response.read(65536), target=model)
        for line in response:
            line = line.decode().strip()
            if not line.startswith("data:"):
                continue
            value = line[5:].strip()
            if value == "[DONE]":
                done = True
                break
            data = json.loads(value)
            router = data.get("x_router")
            if data.get("error") or (
                isinstance(router, dict) and router.get("status") == "incomplete"
            ):
                raise router_error(None, data, target=model, stream=True)
            usage = data.get("usage") or usage
            for choice in data.get("choices", []):
                finish = choice.get("finish_reason") or finish
                delta = choice.get("delta", {})
                if (
                    delta.get("content")
                    or delta.get("reasoning_content")
                    or delta.get("reasoning")
                ):
                    chunks += 1
        if not done or finish not in {"stop", "length"} or not chunks:
            raise RuntimeError(
                "Probe did not receive generated content, terminal finish reason and [DONE]"
            )
        if not usage or any(
            not isinstance(usage.get(k), int) or usage[k] <= 0
            for k in ("prompt_tokens", "completion_tokens")
        ):
            raise RuntimeError(
                "Probe has no valid token usage; benchmark would estimate throughput incorrectly"
            )
        return {
            "checked_at": now(),
            "model": model,
            "usage": usage,
            "finish_reason": finish,
            "content_chunks": chunks,
            "done": done,
        }
    finally:
        connection.close()


class Job:
    def __init__(self, path):
        self.path = Path(path)
        self.directory = self.path.parent
        self.root = self.directory.parent.parent
        self.manifest = read_json(path)
        self.settings = self.manifest["settings"]
        self.mutex = threading.RLock()
        self.cancelled = threading.Event()
        self.external_stop = False
        self.children = set()
        self.log_file = (self.directory / "run.log").open("a", buffering=1)
        self.errors = []
        self.client = client_name(self.manifest["id"])

    def log(self, text):
        with self.mutex:
            line = f"{now()} {text}"
            print(line, flush=True)
            self.log_file.write(line + "\n")

    def save(self):
        with self.mutex:
            self.manifest["updated_at"] = now()
            atomic_json(self.path, self.manifest)
            write_index(self.root)

    def stop(self, *_):
        self.cancelled.set()
        with self.mutex:
            for child in list(self.children):
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def handle_signal(self, *_):
        self.external_stop = True
        self.stop()

    def check_target(self, target, baseline, unhealthy_since=None):
        """Identity before readiness. Health failures get a grace period;
        offline targets, router switches and identity changes propagate."""
        try:
            snap = snapshot(self.settings)
            pinned = baseline["canonical"]
            check_drift(baseline, resolve(snap, target, require_healthy=False, pinned=pinned))
            resolve(snap, target, pinned=pinned)
            return None
        except (ModelOffline, RouterSwitching):
            raise
        except (RuntimeUnavailable, *TRANSIENT) as exc:
            unhealthy_since = unhealthy_since or time.monotonic()
            if time.monotonic() - unhealthy_since >= HEALTH_GRACE:
                raise RuntimeUnavailable(
                    f"Runtime readiness failed for {HEALTH_GRACE}s: {exc}"
                ) from exc
            self.log(f"[{target}] readiness check failed; allowing up to {HEALTH_GRACE}s: {exc}")
            return unhealthy_since

    def settle(self, target, step):
        """Run a pre-request check, waiting (at least ten minutes) while the router switches."""
        deadline = None
        while True:
            try:
                return step()
            except RouterSwitching as exc:
                if deadline is None:
                    deadline = time.monotonic() + ROUTER_SWITCH_WAIT
                    self.log(f"[{target}] {exc}; waiting without sending requests")
                    self.set_progress(target, f"waiting: {exc}")
                if time.monotonic() >= deadline:
                    raise RuntimeUnavailable(
                        f"Router did not finish switching within {ROUTER_SWITCH_WAIT}s: {exc}"
                    ) from exc
                if self.cancelled.wait(10):
                    raise InterruptedError("Job was stopped")

    def set_progress(self, target, text):
        with self.mutex:
            self.manifest["progress"] = f"{target}: {text}"
            self.save()

    def run_child(self, command, target, env, baseline, failure_file=None):
        if self.cancelled.is_set():
            raise InterruptedError("Job was stopped")
        if failure_file and failure_file.exists():
            failure_file.unlink()
        self.log(f"[{target}] command: {' '.join(command)}")
        child = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        with self.mutex:
            self.children.add(child)

        def copy_log():
            for line in child.stdout:
                self.log(f"[{target}] {line.rstrip()}")

        reader = threading.Thread(target=copy_log, daemon=True)
        reader.start()
        try:
            next_check = time.monotonic() + 10
            deadline = time.monotonic() + 12 * 3600
            unhealthy_since = None
            while child.poll() is None:
                if self.cancelled.wait(0.5):
                    raise InterruptedError("Job was stopped")
                if time.monotonic() > deadline:
                    raise RuntimeError("Phase exceeded the 12-hour job deadline")
                if time.monotonic() >= next_check:
                    # A switch raises RouterSwitching: the phase is stopped and excluded.
                    unhealthy_since = self.check_target(target, baseline, unhealthy_since)
                    progress_file = self.directory / target / "progress.json"
                    if progress_file.exists():
                        p = read_json(progress_file)
                        with self.mutex:
                            self.manifest["targets"][target]["progress"] = p
                            self.manifest["progress"] = "; ".join(
                                f"{t}: {i.get('phase', 'preflight')} request {i.get('progress', {}).get('request', '?')}"
                                for t, i in self.manifest["targets"].items()
                            )
                            self.save()
                    next_check = time.monotonic() + 10
            reader.join(timeout=5)
            if self.cancelled.is_set():
                raise InterruptedError("Job was stopped")
            if child.returncode:
                if failure_file and failure_file.exists():
                    failure = read_json(failure_file)
                    raise classified(failure.get("kind"), failure.get("message", ""))
                raise RuntimeError(
                    f"BetterBench exited {child.returncode}; see run.log"
                )
        finally:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                except ProcessLookupError:
                    pass
            reader.join(timeout=5)
            with self.mutex:
                self.children.discard(child)

    def measure(self, target, phase, baseline, launch, validate=lambda: None):
        """Run one phase. A phase that overlaps a router drain is excluded:
        its outputs move to excluded/, switching time never enters a
        measurement, and the phase runs again once the router accepts
        requests with the run's model unchanged."""
        for attempt in range(1, MAX_PHASE_ATTEMPTS + 1):
            started = now()
            try:
                launch()
                validate()
                overlapped = drains_overlapping(self.root, started, now())
                if overlapped:
                    raise RouterSwitching(
                        "router drained during the phase ("
                        + (overlapped[-1].get("reason") or "configuration switch")
                        + ")"
                    )
                return
            except RouterSwitching as exc:
                self.exclude(target, phase, attempt, started, exc)
                if attempt == MAX_PHASE_ATTEMPTS:
                    raise RuntimeUnavailable(
                        f"{phase} overlapped a router drain {attempt} times; no valid measurement"
                    ) from exc
                self.log(
                    f"[{target}] {phase} excluded: it overlapped a router drain ({exc}); "
                    "waiting for the router, then measuring it again"
                )
                self.set_progress(target, f"waiting: {exc}")
                # At least ten minutes for the switch, then the same identity
                # checks as before every request. Never another model.
                wait_for_runtime(self.settings, target, baseline)

    def exclude(self, target, phase, attempt, started, exc):
        target_dir = self.directory / target
        destination = target_dir / "excluded" / f"{phase}-{attempt}"
        destination.mkdir(parents=True, exist_ok=True)
        for path in target_dir.glob(phase + ".*"):
            if path.is_file():
                os.replace(path, destination / path.name)
        with self.mutex:
            info = self.manifest["targets"][target]
            info.setdefault("excluded_phases", []).append(
                {
                    "phase": phase,
                    "attempt": attempt,
                    "started_at": started,
                    "excluded_at": now(),
                    "reason": str(exc),
                    "evidence": str(destination.relative_to(self.directory)),
                }
            )
            if phase + ".html" in info.get("reports", []):
                info["reports"].remove(phase + ".html")
            self.save()

    def target(self, target):
        target_dir = self.directory / target
        target_dir.mkdir(exist_ok=True)
        if self.cancelled.is_set():
            raise InterruptedError("Job was stopped")

        def prepare():
            before = snapshot(self.settings)
            require_idle(before, [target], all_services=False)
            baseline = resolve(before, target, pinned=self.manifest["resolved"][target]["canonical"])
            check_drift(self.manifest["resolved"][target], baseline)
            return before, baseline

        before, baseline = self.settle(target, prepare)
        atomic_json(target_dir / "before.json", before)
        # The request gate in invoke.py checks this pinned identity before
        # every measured request.
        gate = target_dir / "gate.json"
        atomic_json(gate, {"settings": self.settings, "target": target, "baseline": baseline})
        failure_file = target_dir / "failure.json"
        with self.mutex:
            self.manifest["targets"][target] = {
                "canonical": baseline["canonical"],
                "status": "running",
                "phase": "compatibility",
                "reports": [],
            }
            self.save()
        self.log(
            f"[{target}] checking streaming compatibility for {baseline['canonical']}"
        )
        probe_env = dict(
            os.environ,
            BETTERBENCH_CLIENT_NAME=self.client,
            BB_FAILURE_FILE=str(failure_file),
        )
        # Run the probe as a child too: container stop immediately closes its socket.
        probe_cmd = [
            sys.executable,
            "/app/worker.py",
            "--probe",
            self.settings["endpoint"],
            baseline["canonical"],
            str(target_dir / "compatibility.json"),
        ]
        self.measure(
            target,
            "compatibility",
            baseline,
            lambda: self.run_child(probe_cmd, target, probe_env, baseline, failure_file),
        )
        profile = self.manifest.get("profile_spec", {}).get("spec") or read_json(
            Path("/app/profiles") / (self.manifest["profile"] + ".json")
        )
        parameters = self.manifest.get("profile_spec", {}).get("parameters", {})
        for phase in profile["phases"]:
            cfg = dict(profile["config"])
            cfg.update(
                {
                    k: parameters[k]
                    for k in ("temperature", "top_p", "seed")
                    if k in parameters
                }
            )
            cfg["run_single_stream"] = phase == "decode"
            cfg["run_prefill"] = phase == "prefill"
            cfg["run_concurrency"] = False
            # Limits come from the pinned model's published context window.
            cfg["max_model_len"] = baseline["context"]
            cfg["prefill_ctx_margin"] = baseline["reserve"] + 1024
            cfg_path = target_dir / (phase + "-config.json")
            atomic_json(cfg_path, cfg)
            with self.mutex:
                self.manifest["targets"][target]["phase"] = phase
                self.save()
            result = target_dir / (phase + ".json")
            cmd = [
                sys.executable,
                "/app/invoke.py",
                "run",
                "--endpoint",
                self.settings["endpoint"],
                "--model",
                baseline["canonical"],
                "--config",
                str(cfg_path),
                "--" + phase,
                "--out",
                str(result),
                "--no-update-check",
                "--name",
                self.manifest["id"],
                "--note",
                f"service={target}",
                "--note",
                f"profile={self.manifest['profile']}",
                "--note",
                f"mode={self.manifest['mode']}",
                "--note",
                "reasoning=" + parameters.get("reasoning_effort", "default"),
                "--note",
                "top_k=server-default-router-does-not-forward",
                "--note",
                "measurement=routed-API-including-router-overhead",
            ]
            if phase == "decode":
                cmd.extend(["--categories", *profile["categories"]])
                if self.manifest["profile"] == "smoke":
                    cmd.append("--quick")
            effort = parameters.get("reasoning_effort", "default")
            extra = (
                {}
                if effort == "default"
                else {"reasoning_effort": "none" if effort == "off" else effort}
            )
            env = dict(
                os.environ,
                BB_PROGRESS_FILE=str(target_dir / "progress.json"),
                BB_EXTRA_BODY=json.dumps(extra),
                BB_GATE_FILE=str(gate),
                BB_TARGET=target,
                BB_FAILURE_FILE=str(failure_file),
                BETTERBENCH_CLIENT_NAME=self.client,
            )
            if parameters.get("max_tokens") is not None:
                env["BB_MAX_TOKENS"] = str(parameters["max_tokens"])

            def validate():
                validate_results(read_json(result), cfg, profile["categories"])
                # Identity only: a finished phase needs the same model, not a ready one.
                check_drift(baseline, resolve(
                    snapshot(self.settings), target, require_healthy=False,
                    pinned=baseline["canonical"]))

            self.measure(
                target,
                phase,
                baseline,
                lambda: self.run_child(cmd, target, env, baseline, failure_file),
                validate,
            )
            with self.mutex:
                self.manifest["targets"][target]["reports"].append(phase + ".html")
                self.save()

        def finish():
            after = snapshot(self.settings)
            check_drift(baseline, resolve(
                after, target, require_healthy=False, pinned=baseline["canonical"]))
            return after

        after = self.settle(target, finish)
        atomic_json(target_dir / "after.json", after)
        with self.mutex:
            self.manifest["targets"][target].update(
                status="completed", phase="finished"
            )
            self.save()
        self.log(f"[{target}] complete; reports validated")

    def run(self):
        locks = []
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)
        try:
            for target in sorted(self.manifest["requested_targets"]):
                path = self.root / "locks" / (target + ".lock")
                path.parent.mkdir(exist_ok=True)
                lock = path.open("a")
                locks.append(lock)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError(
                        f"Another BetterBench job holds the {target} service lock"
                    )
            self.settle(
                ", ".join(self.manifest["requested_targets"]),
                lambda: require_idle(
                    snapshot(self.settings), self.manifest["requested_targets"]
                ),
            )
            self.manifest.update(status="running", started_at=now())
            self.save()
            self.log(
                f"Starting {self.manifest['profile']} ({self.manifest['mode']}); reasoning uses server defaults"
            )
            if self.manifest["mode"] == "parallel":
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [
                        pool.submit(self.target, target)
                        for target in self.manifest["requested_targets"]
                    ]
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            self.errors.append(e)
                            self.stop()
                if self.errors:
                    # Keep the most serious classification (identity first).
                    kinds = [error_kind(e) for e in self.errors]
                    kind = next(
                        (k for k in ("configuration_changed", "model_offline") if k in kinds),
                        None,
                    )
                    raise classified(kind, "; ".join(error_message(e) for e in self.errors))
            else:
                for target in self.manifest["requested_targets"]:
                    self.target(target)
            if self.cancelled.is_set():
                raise InterruptedError("Job was stopped")
            self.manifest.update(
                status="completed", progress="All requested reports passed validation"
            )
        except BaseException as e:
            stopped = (
                self.external_stop
                or isinstance(e, (InterruptedError, KeyboardInterrupt))
                or (self.directory / "stop-requested").exists()
            )
            # Only a real identity change makes a run invalid; an offline
            # model or an unfinished switch fails it (rerun when available).
            state = (
                "interrupted"
                if stopped
                else (
                    "invalid"
                    if isinstance(e, ModelConfigurationChanged) or "changed during" in str(e)
                    else "failed"
                )
            )
            self.manifest.update(status=state, error=error_message(e))
            if error_kind(e) and not stopped:
                self.manifest["error_kind"] = error_kind(e)
            for info in self.manifest["targets"].values():
                if info.get("status") == "running":
                    info["status"] = state
            self.log(f"{state.upper()}: {error_message(e)}")
            self.stop()
        finally:
            self.manifest["finished_at"] = now()
            self.save()
            self.log(f"Job {self.manifest['id']}: {self.manifest['status']}")
            for lock in locks:
                lock.close()
            self.log_file.close()
        return 0 if self.manifest["status"] == "completed" else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        try:
            atomic_json(
                sys.argv[4],
                compatibility_probe(
                    sys.argv[2], sys.argv[3], os.environ.get("BETTERBENCH_CLIENT_NAME")
                ),
            )
        except Exception as exc:
            if os.environ.get("BB_FAILURE_FILE") and error_kind(exc):
                atomic_json(
                    os.environ["BB_FAILURE_FILE"],
                    {"kind": error_kind(exc), "message": error_message(exc)},
                )
            raise
    else:
        sys.exit(Job(sys.argv[1]).run())
