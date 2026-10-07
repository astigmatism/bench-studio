"""Trusted controller. Only this service has Docker access."""

import fcntl
import json
import os
import signal
import subprocess
import threading
import time
from common import (
    HEALTH_GRACE,
    ROUTER_SWITCH_WAIT,
    TRANSIENT,
    now,
    read_json,
    atomic_json,
    ModelConfigurationChanged,
    ModelOffline,
    RouterSwitching,
    RuntimeUnavailable,
    capabilities,
    check_drift,
    classified,
    error_kind,
    error_message,
    note_router_state,
    resolve,
    require_idle,
    safe_target,
    snapshot,
)
from . import config, db, results, generation

MANAGED = "io.bench-studio.run"
STOP = False
ROLLOUT = None
# Router subscription (studio.router_events.Subscriber), started by main().
ROUTER = None
# Set by a new router revision or a finished run: re-check without delay.
WAKE = threading.Event()
# (run ID, status) -> monotonic time of its last router/runtime check. Cleared
# on every new router revision so active and queued runs are re-checked at once.
CHECKED = {}
_CYCLE = {"active": False, "snapshot": None}


def check_interval():
    """Slower periodic checks: 30 s while the event stream is live (runtime-only
    facts such as container restarts are not evented), 10 s while it is down."""
    return 30 if ROUTER is not None and ROUTER.streaming else 10


def due(m, interval=None):
    # Keyed by status too: a run that changes state is checked at once.
    last = CHECKED.get((m["id"], m["status"]))
    return last is None or time.monotonic() - last >= (interval or check_interval())


def checked(m):
    CHECKED[(m["id"], m["status"])] = time.monotonic()


def router_changed(_doc=None):
    CHECKED.clear()
    WAKE.set()


def cycle_snapshot():
    """One router/runtime snapshot per scheduler cycle, shared by every run.

    A failed fetch is remembered for the cycle too, so an unreachable router
    costs one timeout per cycle rather than one per run.
    """
    if not _CYCLE["active"]:
        return snapshot(config.SETTINGS)
    if _CYCLE["snapshot"] is None:
        try:
            _CYCLE["snapshot"] = snapshot(config.SETTINGS)
        except Exception as exc:
            _CYCLE["snapshot"] = exc
    if isinstance(_CYCLE["snapshot"], Exception):
        raise _CYCLE["snapshot"]
    return _CYCLE["snapshot"]


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
    from .session_catalog import vision_support

    unavailable = "The router and AI Runtime APIs do not expose this setting"
    evidence = {
        "collected_at": now(),
        "engine_args": {},
        "engine_args_unavailable": {},
        "backend_defaults": {},
        "vision": {},
    }
    services = {s["model"]: s for s in snap["runtime"].get("services", [])}
    caps = capabilities(snap)
    evidence["router"] = {
        "revision": caps.get("revision"),
        "configuration": caps.get("configuration"),
        "offline_services": caps.get("offline_services", []),
    }
    for row in caps.get("models", []):
        meta = row.get("metadata") or {}
        canonical = row.get("id") or meta.get("upstream_model")
        service = services.get(canonical, {})
        exposed_args = service.get("engine_args")
        args = exposed_args if isinstance(exposed_args, list) else []
        evidence["engine_args"][canonical] = (
            args if isinstance(exposed_args, list) else None
        )
        if not isinstance(exposed_args, list):
            evidence["engine_args_unavailable"][canonical] = unavailable
        defaults = meta.get("default_generation_settings")
        if defaults is None:
            defaults = service.get("default_generation_settings")
        for key in dict.fromkeys([safe_target(row["id"]), row.get("service")]):
            if key:
                # Runs target service IDs; older runs targeted canonical IDs.
                evidence["backend_defaults"][key] = (
                    defaults if isinstance(defaults, dict) else {"unavailable": unavailable}
                )

        def argument(key):
            if key in args and args.index(key) + 1 < len(args):
                return args[args.index(key) + 1]
            return next(
                (v.split("=", 1)[1] for v in args if v.startswith(key + "=")), None
            )

        value = {
            "advertised": vision_support(meta),
            "input_modalities": meta.get("input_modalities"),
            "projector_path": argument("--mmproj"),
            "projector_revision": meta.get("mmproj_revision"),
            "projector_sha256": meta.get("mmproj_sha256"),
            "offload": "cpu"
            if "--no-mmproj-offload" in args
            else meta.get("mmproj_offload"),
            "device": argument("--mmproj-device") or service.get("vision_device"),
            "image_min_tokens": argument("--image-min-tokens"),
            "image_max_tokens": argument("--image-max-tokens"),
            "encoder_latency_ms": None,
        }
        for key in dict.fromkeys(
            [row["id"], row.get("service"), *(row.get("aliases") or meta.get("aliases", []))]
        ):
            if key:
                evidence["vision"][key] = value
    controller = inspect("bench-studio-runner")
    evidence["runner_image"] = controller.get("Image") if controller else None
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
    image = image or m.get("host", {}).get(
        "verifier_image" if role == "verify" else "worker_image"
    )
    if not image:
        raise RuntimeError("A pinned execution image is required before starting")
    source = config.DATA if not target else config.DATA / "runs" / m["id"] / target
    destination = "/data" if not target else "/task"
    args = [
        "create",
        "--name",
        name,
        "--label",
        f"{MANAGED}={m['id']}",
        "--label",
        "io.bench-studio.project=" + os.environ.get("PROJECT_DIR", str(config.ROOT)),
        "--label",
        "io.service-portal.hidden=true",
        "--init",
        "--user",
        config.WORKER_USER,
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
    if m.get("family") in {"session", "vision"}:
        from .session_runner import stop

        stop(m)
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
    ids = docker("ps", "-q", "--filter", f"label={MANAGED}={m['id']}").stdout.split()
    if ids:
        docker("stop", "--time", "10", *ids)


def finish(m, status, error=None):
    m.update(
        status=status,
        finished_at=now(),
        progress="Complete" if status == "completed" else status.capitalize(),
    )
    if error:
        m["error"] = error_message(error) if isinstance(error, BaseException) else str(error)
        if isinstance(error, BaseException) and error_kind(error):
            m["error_kind"] = error_kind(error)
    # A finished run frees the machine: check queued runs without delay.
    CHECKED.clear()
    WAKE.set()
    for t in m.get("requested_targets", []):
        m.setdefault("targets", {}).setdefault(t, {}).update(
            status=status, phase="finished"
        )
    record(m)
    atomic_json(config.DATA / "runs" / m["id"] / "studio-manifest.json", m)
    log(m, f"{status.upper()}: {error or 'Results saved'}")
    if status == "completed" and m.get("family") in {"session", "vision"}:
        from .session_catalog import qualify_run

        qualify_run(m)


def start(m, snap):
    from datetime import datetime

    path = config.DATA / "runs" / m["id"]
    path.mkdir(parents=True, exist_ok=True)
    m.update(
        status="starting",
        started_at=now(),
        host=host_evidence(snap),
        stage="generation",
        workers={},
        progress="Preparing benchmark",
    )
    m["queue_seconds"] = max(
        0,
        (
            datetime.fromisoformat(m["started_at"])
            - datetime.fromisoformat(m["created_at"])
        ).total_seconds(),
    )
    if m["family"] == "quality" and not m["host"].get("verifier_image"):
        raise RuntimeError("The pinned coding verifier image is unavailable")
    m["generation"] = generation.snapshot(m)
    atomic_json(path / "manifest.json", m)
    record(m)
    log(m, "Starting " + m["profile"] + " / " + m["mode"])
    if m["family"] in {"session", "vision"}:
        from .session_runner import launch

        launch(m)
    elif m["family"] == "agent":
        from .agent import launch_agent

        launch_agent(m)
    else:
        module = (
            "/app/worker.py"
            if m["family"] == "speed"
            else "/app/studio/quality_worker.py"
        )
        create_worker(
            m, "generate", ["python", module, f"/data/runs/{m['id']}/manifest.json"]
        )
    m["status"] = "running"
    record(m)


def check_current(m):
    """Re-check a running run's pinned identity and readiness.

    A router switch pauses the run (at least ten minutes) without a verdict;
    an offline target raises ModelOffline (failed, not invalid); a different
    identity raises ModelConfigurationChanged (invalid). Never another model.
    """
    try:
        snap = cycle_snapshot()
        # Validate every identity before readiness; an unhealthy peer must not hide drift.
        for t in m["requested_targets"]:
            pinned = m["resolved"][t]["canonical"]
            check_drift(m["resolved"][t], resolve(snap, t, require_healthy=False, pinned=pinned))
        for t in m["requested_targets"]:
            resolve(snap, t, pinned=m["resolved"][t]["canonical"])
    except ModelOffline:
        raise
    except RouterSwitching as exc:
        note_router_state(config.DATA, False, str(exc))
        m.pop("health_unavailable_since", None)
        first = m.setdefault("switching_since", time.time())
        m["health_warning"] = (
            f"{exc}: requests are paused (up to {ROUTER_SWITCH_WAIT // 60} minutes) "
            "and the model is checked again afterwards; no other model is used"
        )
        record(m)
        if time.time() - first >= ROUTER_SWITCH_WAIT:
            raise RuntimeUnavailable(
                f"Router did not finish switching within {ROUTER_SWITCH_WAIT}s: {exc}"
            ) from exc
        return None
    except (RuntimeUnavailable, *TRANSIENT) as exc:
        m.pop("switching_since", None)
        first = m.setdefault("health_unavailable_since", time.time())
        m["health_warning"] = (
            f"Runtime readiness check failed; allowing up to {HEALTH_GRACE}s to recover without replaying requests: {exc}"
        )
        record(m)
        if time.time() - first >= HEALTH_GRACE:
            raise RuntimeUnavailable(
                f"Runtime readiness failed for {HEALTH_GRACE}s: " + str(exc)
            ) from exc
        return None
    note_router_state(config.DATA, True)
    m.pop("health_unavailable_since", None)
    m.pop("switching_since", None)
    m.pop("health_warning", None)
    count = dict(snap["runtime"].get("maintenance", {}))
    load = capabilities(snap).get("load")
    if load:
        count = {
            "active_requests": sum(v.get("active", 0) for v in load.values()),
            "queued_requests": sum(v.get("queued", 0) for v in load.values()),
        }
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
    if m["family"] in {"session", "vision"}:
        from .session_runner import poll as poll_session

        # Waiting sessions hold no inference reservation. Identity is rechecked at resume.
        if m["status"] not in db.WAITING and due(m):
            check_current(m)
            checked(m)
        poll_session(m)
        return
    if due(m):
        check_current(m)
        checked(m)
    if m["family"] == "agent":
        from .agent import poll_agent

        poll_agent(m)
        return
    path = config.DATA / "runs" / m["id"] / "manifest.json"
    worker = read_json(path) if path.exists() else {}
    if worker:
        for k in (
            ["progress", "targets", "error", "error_kind"]
            if m.get("stage") == "generation"
            else ["error", "error_kind"]
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
            kind = m.get("error_kind")
            if not kind and path.exists() and read_json(path).get("status") == "invalid":
                kind = "configuration_changed"
            raise classified(
                kind,
                m.get("error")
                or f"{info['role']} worker exited {state['State']['ExitCode']}",
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
                image=m["host"]["verifier_image"],
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
    _CYCLE.update(active=True, snapshot=None)
    try:
        _cycle()
    finally:
        _CYCLE.update(active=False, snapshot=None)


def _cycle():
    if ROLLOUT:
        ROLLOUT.sync_control()
    active = [m for m in db.runs() if m["status"] in db.ACTIVE | db.WAITING]
    for m in active:
        try:
            poll(m)
        except Exception as e:
            try:
                stop_owned(m)
            except Exception as stop_error:
                log(m, "Cleanup error: " + str(stop_error))
            # Only a real identity change is invalid. An offline model fails
            # the run (its identity did not change); the user reruns it.
            state = (
                "invalid"
                if isinstance(e, ModelConfigurationChanged)
                else ("interrupted" if "disappeared" in str(e) else "failed")
            )
            finish(m, state, e)
    if (
        STOP
        or any(m["status"] in db.ACTIVE for m in db.runs())
        or (config.DATA / ".maintenance").exists()
    ):
        return
    if ROLLOUT and ROLLOUT.tick():
        return
    queued = sorted(
        (m for m in db.runs() if m["status"] in {"queued", "resume_queued"}),
        key=lambda m: (m["status"] != "resume_queued", m["created_at"]),
    )
    for m in queued:
        if not due(m, QUEUED_CHECK_SECONDS):
            if m.get("waiting_for"):
                continue
            return
        checked(m)
        # A run waiting for an offline model does not hold back later runs.
        if consider(m) != "waiting":
            return


# Queued runs wait on router load, which is polled (load never changes the
# capabilities revision); new revisions and finished runs re-check at once.
QUEUED_CHECK_SECONDS = 10


def consider(m):
    """Start a queued run when its pinned model is current and the machine idle.

    Returns "waiting" while the run's target is offline in the current
    configuration; it starts automatically when the target returns with the
    same identity, and is blocked if it returns as a different model.
    """
    try:
        if m.get("revision", config.REVISION) != config.REVISION:
            if m["status"] == "resume_queued":
                stop_owned(m)
                finish(
                    m,
                    "interrupted",
                    "Application changed during review; session evidence retained",
                )
                return "hold"
            m.update(
                status="blocked",
                progress="Application version changed while queued. Review and run again.",
            )
            record(m)
            return "hold"
        snap = cycle_snapshot()
        for t in m["requested_targets"]:
            pinned = m["resolved"][t]["canonical"]
            observed = resolve(snap, t, require_healthy=False, pinned=pinned)
            # A queued run must find the same model; a session resumed after
            # review must also find the same running instance.
            check_drift(
                m["resolved"][t], observed, instance=m["status"] == "resume_queued"
            )
        require_idle(snap, m["requested_targets"])
        current = {
            t: resolve(snap, t, pinned=m["resolved"][t]["canonical"])
            for t in m["requested_targets"]
        }
    except ModelOffline as exc:
        if m["status"] == "resume_queued":
            stop_owned(m)
            finish(m, "failed", exc)
            return "hold"
        if m.get("progress") != exc.waiting:
            m["progress"] = exc.waiting
            m["waiting_for"] = {
                "target": exc.target,
                "configuration": exc.configuration,
                "reason": exc.reason,
                "since": m.get("waiting_for", {}).get("since") or now(),
            }
            record(m)
            log(m, exc.waiting + "; it starts when the model returns with the same identity")
        return "waiting"
    except RouterSwitching as exc:
        text = f"waiting: {exc}"
        if m.get("progress") != text:
            m["progress"] = text
            record(m)
        return "hold"
    except ModelConfigurationChanged:
        if m["status"] == "resume_queued":
            stop_owned(m)
            finish(m, "invalid", "Model/runtime configuration changed during review")
        else:
            m.pop("waiting_for", None)
            m.update(
                status="blocked",
                progress="Selected model or configuration changed. Review and run again.",
            )
            record(m)
        return "hold"
    except Exception as e:
        if m.get("progress") != str(e):
            m["progress"] = str(e)
            record(m)
        return "hold"
    if m.pop("waiting_for", None):
        log(m, "Model available again with the same identity")
    # Same flock is held by the updater from preflight to health validation.
    with (config.DATA / ".execution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "hold"
        with db.transaction() as c:
            latest = db.unpack(
                c.execute("SELECT document FROM runs WHERE id=?", (m["id"],)).fetchone()
            )
            if (
                STOP
                or latest["status"] not in {"queued", "resume_queued"}
                or (config.DATA / ".maintenance").exists()
            ):
                return "hold"
            if latest["status"] == "resume_queued":
                latest.update(
                    status="running",
                    resume_review=latest["pending_review"],
                    progress="Resuming after review",
                )
                db.update_run(latest, c=c)
                return "hold"
            repin(m, current)
            m["status"] = "starting"
            db.update_run(m, c=c)
        try:
            start(m, snap)
        except Exception as e:
            finish(m, "failed", e)
    return "hold"


def repin(m, current):
    """Record the running instance a queued run starts on.

    The model identity (canonical ID, revision, context, engine) matched the
    one pinned at queue time; only the container instance may differ, for
    example after a solo configuration stopped and restarted Nighttime. The
    queue-time record is kept, and from now on the instance must not change.
    """
    for t, resolved in current.items():
        before = m["resolved"][t]
        if any(
            before.get("service", {}).get(k) != resolved["service"].get(k)
            for k in ("id", "started_at", "restart_count")
        ):
            m.setdefault("resolved_at_queue", {})[t] = before
        m["resolved"][t] = {**resolved, "alias": t}


def shutdown_sessions():
    for m in db.runs():
        if (
            m.get("family") in {"session", "vision"}
            and m["status"] in db.ACTIVE | db.WAITING
        ):
            try:
                stop_owned(m)
                finish(
                    m,
                    "interrupted",
                    "Controller stopped; session evidence retained without replay",
                )
            except Exception as exc:
                log(m, "Session shutdown cleanup failed: " + str(exc))


def start_router_watch(settings=None):
    """Subscribe to router changes for the runner's lifetime (contract §4).

    Never blocks or fails startup: an unreachable router is retried in the
    background while the scheduler runs degraded on its periodic checks.
    """
    global ROUTER
    from .router_events import Subscriber

    ROUTER = Subscriber(settings or config.SETTINGS, on_revision=router_changed).start()
    return ROUTER


def request_shutdown(*_):
    global STOP
    STOP = True
    WAKE.set()


def main():
    global ROLLOUT
    db.initialize()
    results.import_legacy()
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    with (config.DATA / ".runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from .session_setup import SessionSetup

        ROLLOUT = SessionSetup()
        start_router_watch()
        try:
            while not STOP:
                try:
                    cycle()
                except Exception as e:
                    print("Controller error:", e, flush=True)
                # A new router revision wakes the scheduler immediately.
                WAKE.wait(3)
                WAKE.clear()
        finally:
            try:
                if ROUTER:
                    ROUTER.stop()
                if ROLLOUT:
                    ROLLOUT.stop()
            finally:
                shutdown_sessions()


if __name__ == "__main__":
    main()
