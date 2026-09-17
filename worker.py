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
    atomic_json,
    check_drift,
    get_json,
    identity,
    now,
    read_json,
    require_idle,
    resolve,
    snapshot,
    validate_results,
    write_index,
)


def compatibility_probe(endpoint, model):
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
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(
                f"Compatibility probe HTTP {response.status}: {response.read(500)!r}"
            )
        for line in response:
            line = line.decode().strip()
            if not line.startswith("data:"):
                continue
            value = line[5:].strip()
            if value == "[DONE]":
                done = True
                break
            data = json.loads(value)
            if data.get("error"):
                raise RuntimeError(f"SSE error: {data['error']}")
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

    def run_child(self, command, target, env, baseline):
        if self.cancelled.is_set():
            raise InterruptedError("Job was stopped")
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
            while child.poll() is None:
                if self.cancelled.wait(0.5):
                    raise InterruptedError("Job was stopped")
                if time.monotonic() > deadline:
                    raise RuntimeError("Phase exceeded the 12-hour job deadline")
                if time.monotonic() >= next_check:
                    check_drift(baseline, resolve(snapshot(self.settings), target))
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

    def target(self, target):
        target_dir = self.directory / target
        target_dir.mkdir(exist_ok=True)
        if self.cancelled.is_set():
            raise InterruptedError("Job was stopped")
        before = snapshot(self.settings)
        require_idle(before, [target], all_services=False)
        baseline = resolve(before, target)
        check_drift(self.manifest["resolved"][target], baseline)
        atomic_json(target_dir / "before.json", before)
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
        # Run the probe as a child too: container stop immediately closes its socket.
        probe_cmd = [
            sys.executable,
            "/app/worker.py",
            "--probe",
            self.settings["endpoint"],
            baseline["canonical"],
            str(target_dir / "compatibility.json"),
        ]
        self.run_child(probe_cmd, target, os.environ.copy(), baseline)
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
                "reasoning=server-default",
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
            )
            if parameters.get("max_tokens") is not None:
                env["BB_MAX_TOKENS"] = str(parameters["max_tokens"])
            self.run_child(cmd, target, env, baseline)
            validate_results(read_json(result), cfg, profile["categories"])
            check_drift(baseline, resolve(snapshot(self.settings), target))
            with self.mutex:
                self.manifest["targets"][target]["reports"].append(phase + ".html")
                self.save()
        after = snapshot(self.settings)
        check_drift(baseline, resolve(after, target))
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
            start = snapshot(self.settings)
            require_idle(start, self.manifest["requested_targets"])
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
                            self.errors.append(str(e))
                            self.stop()
                if self.errors:
                    raise RuntimeError("; ".join(self.errors))
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
            state = (
                "interrupted"
                if stopped
                else ("invalid" if "changed during" in str(e) else "failed")
            )
            self.manifest.update(status=state, error=str(e))
            for info in self.manifest["targets"].values():
                if info.get("status") == "running":
                    info["status"] = state
            self.log(f"{state.upper()}: {e}")
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
        atomic_json(sys.argv[4], compatibility_probe(sys.argv[2], sys.argv[3]))
    else:
        sys.exit(Job(sys.argv[1]).run())
