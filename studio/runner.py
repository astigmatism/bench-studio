"""Trusted controller. Only this service has Docker access."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from common import (
    now,
    read_json,
    atomic_json,
    identity,
    resolve,
    require_idle,
    snapshot,
    get_json,
)
from . import config, db, results

MANAGED = "io.bench-studio.run"
STOP = False


def docker(*args, check=True):
    p = subprocess.run(
        ["docker", *map(str, args)], capture_output=True, text=True, timeout=90
    )
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return p


def inspect(name):
    p = docker("inspect", name, check=False)
    if p.returncode:
        if "no such" in p.stderr.lower():
            return None
        raise RuntimeError(p.stderr)
    return json.loads(p.stdout)[0]


def log(m, text):
    path = config.DATA / "runs" / m["id"]
    path.mkdir(parents=True, exist_ok=True)
    with (path / "run.log").open("a") as f:
        f.write(now() + " " + text + "\n")
    print(m["id"], text, flush=True)


def record(m):
    # Cancellation is an API-owned flag; never erase it with an older snapshot.
    with db.transaction() as c:
        latest = db.unpack(
            c.execute("SELECT document FROM runs WHERE id=?", (m["id"],)).fetchone()
        )
        if latest and latest.get("cancel_requested"):
            m["cancel_requested"] = True
        db.update_run(m, c=c)


def host_evidence(snap):
    evidence = {"collected_at": now(), "engine_args": {}, "backend_defaults": {}}
    for service in snap["runtime"]["services"]:
        d = inspect(service["container_name"])
        evidence["engine_args"][service["model"]] = (
            d["Config"].get("Cmd") if d else None
        )
    for alias, port in [("daytime", 18080), ("nighttime", 18081)]:
        try:
            evidence["backend_defaults"][alias] = get_json(
                f"http://127.0.0.1:{port}/props"
            ).get("default_generation_settings", {})
        except Exception as e:
            evidence["backend_defaults"][alias] = {"unavailable": str(e)}
    for role, image in [
        ("worker", config.WORKER_IMAGE),
        ("verifier", config.VERIFIER_IMAGE),
    ]:
        p = docker("image", "inspect", image, "--format", "{{.Id}}", check=False)
        evidence[role + "_image"] = p.stdout.strip() if p.returncode == 0 else None
    return evidence


def create_worker(m, role, command, *, target=None, image=None, network="bridge"):
    suffix = "-" + target if target else ""
    name = "bench-studio-" + m["id"].lower() + "-" + role + suffix
    if not all(c.isalnum() or c in "._-" for c in name):
        raise ValueError(
            "Model alias must use letters, numbers, dot, dash, or underscore"
        )
    image = image or config.WORKER_IMAGE
    source = config.DATA if not target else config.DATA / "runs" / m["id"] / target
    destination = "/data" if not target else "/task"
    args = [
        "create",
        "--name",
        name,
        "--label",
        f'{MANAGED}={m["id"]}',
        "--label",
        "io.service-portal.hidden=true",
        "--init",
        "--user",
        "1000:1000",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "4g",
        "--cpus",
        "2",
        "--pids-limit",
        "256",
        "--network",
        network,
        "--tmpfs",
        "/tmp:rw,nosuid,size=1g,mode=1777",
        "-e",
        "HOME=/tmp",
        "-e",
        "PYTHONUNBUFFERED=1",
        "-e",
        "OPENBLAS_NUM_THREADS=1",
        "-e",
        "OMP_NUM_THREADS=1",
        "--mount",
        f"type=bind,src={source},dst={destination}",
        image,
        *command,
    ]
    old = inspect(name)
    if old is None:
        docker(*args)
    elif old["Config"].get("Labels", {}).get(MANAGED) != m["id"]:
        raise RuntimeError("Worker name is owned by another application")
    m.setdefault("workers", {})[name] = {"role": role, "target": target}
    record(
        m
    )  # Persist ownership before starting. Restart reconciliation starts a created worker once.
    docker("start", name)
    return name


def stop_owned(m):
    if m.get("family") == "agent":
        from .agent import stop_agent

        stop_agent(m)
    for name in m.get("workers", {}):
        state = inspect(name)
        if (
            state
            and state["Config"].get("Labels", {}).get(MANAGED) == m["id"]
            and state["State"]["Running"]
        ):
            docker("stop", "--time", "10", name)
    # Agent environments are labelled by our Harbor environment adapter.
    ids = docker("ps", "-q", "--filter", f'label={MANAGED}={m["id"]}').stdout.split()
    if ids:
        docker("stop", "--time", "10", *ids)


def finish(m, status, error=None):
    m.update(
        status=status,
        finished_at=now(),
        progress="Complete" if status == "completed" else status.capitalize(),
    )
    if error:
        m["error"] = str(error)
    record(m)
    atomic_json(config.DATA / "runs" / m["id"] / "studio-manifest.json", m)
    log(m, f'{status.upper()}: {error or "Results saved"}')


def start(m, snap):
    path = config.DATA / "runs" / m["id"]
    path.mkdir(parents=True, exist_ok=True)
    m.update(
        status="starting",
        started_at=now(),
        host=host_evidence(snap),
        stage="generation",
        workers={},
    )
    atomic_json(path / "manifest.json", m)
    record(m)
    log(m, "Starting " + m["profile"] + " / " + m["mode"])
    if m["family"] == "agent":
        from .agent import launch_agent

        launch_agent(m)
    else:
        module = (
            "/app/worker.py"
            if m["family"] == "speed"
            else "/app/studio/quality_worker.py"
        )
        create_worker(
            m, "generate", ["python", module, f'/data/runs/{m["id"]}/manifest.json']
        )
    m["status"] = "running"
    record(m)


def check_current(m):
    snap = snapshot(config.SETTINGS)
    for t in m["requested_targets"]:
        if identity(resolve(snap, t)) != identity(m["resolved"][t]):
            raise RuntimeError("Model/runtime configuration changed during the run")
    count = snap["runtime"].get("maintenance", {})
    own = sum(
        1 for w in m.get("workers", {}).values() if w["role"] in ["generate", "agent"]
    )
    permitted = len(m["requested_targets"]) if m["mode"] == "parallel" else 1
    if (
        count.get("queued_requests", 0) > 0
        or count.get("active_requests", 0) > permitted
    ):
        m["load_warning"] = (
            "Other routed activity was detected; this is not an isolated baseline."
        )
    return snap


def poll(m):
    if m.get("cancel_requested"):
        m["status"] = "stopping"
        record(m)
        stop_owned(m)
        finish(m, "cancelled", "Stopped by user; partial artifacts retained")
        return
    check_current(m)
    if m["family"] == "agent":
        from .agent import poll_agent

        poll_agent(m)
        return
    path = config.DATA / "runs" / m["id"] / "manifest.json"
    if path.exists():
        worker = read_json(path)
        for k in (
            ["progress", "targets", "error"]
            if m.get("stage") == "generation"
            else ["error"]
        ):
            if k in worker:
                m[k] = worker[k]
    all_done = True
    for name, info in list(m.get("workers", {}).items()):
        state = inspect(name)
        if state is None:
            raise RuntimeError(
                "Worker disappeared; interrupted without replaying requests"
            )
        if state["State"]["Status"] == "created":
            docker("start", name)
            all_done = False
        elif state["State"]["Running"]:
            all_done = False
        elif state["State"]["ExitCode"] != 0:
            tail = docker("logs", "--tail", "12", name, check=False)
            log(m, (tail.stdout + tail.stderr)[-6000:])
            raise RuntimeError(
                m.get("error")
                or f'{info["role"]} worker exited {state["State"]["ExitCode"]}'
            )
    if not all_done:
        record(m)
        return
    if m["family"] == "speed":
        if worker.get("status") != "completed":
            raise RuntimeError(
                worker.get("error", "Worker finished without validated results")
            )
        finish(m, "completed")
        return
    if m["stage"] == "generation":
        m.update(stage="grading", status="grading", progress="Executing isolated tests")
        record(m)
        for t in m["requested_targets"]:
            create_worker(
                m,
                "verify",
                ["python", "/app/studio/verify.py", "/task"],
                target=t,
                image=config.VERIFIER_IMAGE,
                network="none",
            )
        return
    for t in m["requested_targets"]:
        out = config.DATA / "runs" / m["id"] / t / "result.json"
        if not out.exists():
            raise RuntimeError("Verifier did not produce a result")
        r = read_json(out)
        if r.get("infrastructure_error"):
            raise RuntimeError(r["infrastructure_error"])
        if len(r.get("tasks", [])) != r.get("count"):
            raise RuntimeError("Verifier result count mismatch")
    finish(m, "completed")


def cycle():
    db.set_state(
        "runner", {"updated_at": now(), "revision": config.REVISION, "pid": os.getpid()}
    )
    active = [m for m in db.runs() if m["status"] in db.ACTIVE]
    for m in active:
        try:
            poll(m)
        except Exception as e:
            try:
                stop_owned(m)
            except Exception as stop_error:
                log(m, "Cleanup error: " + str(stop_error))
            state = (
                "invalid"
                if "changed during" in str(e)
                else ("interrupted" if "disappeared" in str(e) else "failed")
            )
            finish(m, state, e)
    if active or (config.DATA / ".maintenance").exists():
        return
    queued = sorted(
        (m for m in db.runs() if m["status"] == "queued"), key=lambda m: m["created_at"]
    )
    if not queued:
        return
    m = queued[0]
    try:
        if m.get("revision", config.REVISION) != config.REVISION:
            m.update(
                status="blocked",
                progress="Application version changed while queued. Review and run again.",
            )
            record(m)
            return
        snap = snapshot(config.SETTINGS)
        for t in m["requested_targets"]:
            if identity(resolve(snap, t)) != identity(m["resolved"][t]):
                m.update(
                    status="blocked",
                    progress="Selected model or configuration changed. Review and run again.",
                )
                record(m)
                return
        require_idle(snap, m["requested_targets"])
    except Exception as e:
        if m.get("progress") != str(e):
            m["progress"] = str(e)
            record(m)
        return
    # Same flock is held by the updater from preflight to health validation.
    with (config.DATA / ".execution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with db.transaction() as c:
            current = db.unpack(
                c.execute("SELECT document FROM runs WHERE id=?", (m["id"],)).fetchone()
            )
            if current["status"] != "queued" or (config.DATA / ".maintenance").exists():
                return
            m["status"] = "starting"
            db.update_run(m, c=c)
        try:
            start(m, snap)
        except Exception as e:
            finish(m, "failed", e)


def main():
    db.initialize()
    results.import_legacy()
    with (config.DATA / ".runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                cycle()
            except Exception as e:
                print("Controller error:", e, flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
