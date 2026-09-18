"""Durable scheduling boundary around the trusted session subprocess."""

import os
import signal
import subprocess
import shutil
import sys
from common import atomic_json, read_json
from . import config
from .session_catalog import attach
from .session_reviews import reviews

_processes = {}


def launch(m):
    from .runner import record, log, docker

    profile = attach(dict(m["profile_spec"]))
    if any(
        profile[k] != m["profile_spec"][k]
        for k in ("task_manifest_hash", "session_image")
    ):
        raise RuntimeError("Session fixtures changed while queued")
    docker("image", "inspect", profile["session_image"])
    root = config.DATA / "runs" / m["id"]
    from .session_catalog import ROOT, tasks, digest_tree

    inputs = root / "session-inputs"
    shutil.copytree(ROOT / "acceptance", inputs / "acceptance")
    shutil.copyfile(ROOT / "manifest.json", inputs / "manifest.json")
    for task in tasks(profile):
        if task.get("image"):
            path = inputs / task["image"]
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / task["image"], path)
    if digest_tree(ROOT) != profile["fixture_source_hash"]:
        raise RuntimeError("Session fixtures changed during input snapshot")
    atomic_json(root / "manifest.json", m)
    with (root / "run.log").open("a") as output:
        child = subprocess.Popen(
            [sys.executable, "-m", "studio.session_job", str(root / "manifest.json")],
            stdout=output,
            stderr=subprocess.STDOUT,
            env=dict(
                os.environ,
                LITELLM_LOCAL_MODEL_COST_MAP="True",
                LITELLM_TELEMETRY="False",
                DO_NOT_TRACK="1",
                STUDIO_CONTROLLER_PID=str(os.getpid()),
            ),
            start_new_session=True,
        )
    _processes[m["id"]] = child
    m["session_pid"] = child.pid
    record(m)
    log(
        m,
        "Session agent started; plans require read-only inspection and explicit review",
    )


def poll(m):
    from .runner import record, finish

    child = _processes.get(m["id"])
    if child is None:
        raise RuntimeError(
            "Session harness disappeared after controller restart; artifacts retained"
        )
    root = config.DATA / "runs" / m["id"]
    if child.poll() is None:
        path = root / "session-progress.json"
        if path.exists():
            progress = read_json(path)
            m["session_progress"] = progress
            m["progress"] = (
                f"{progress.get('attempt_id', '')} · {progress.get('phase', '')}"
            )
            if progress.get("phase") == "review_wait":
                key = progress["review_key"]
                if m.get("resume_review") != key:
                    review = next(
                        (
                            r
                            for r in reviews(m["id"])
                            if f"{r['target']}:{r['attempt_id']}:{r['revision']}" == key
                        ),
                        None,
                    )
                    m["pending_review"] = key
                    m["status"] = (
                        "resume_queued"
                        if review and review.get("decision")
                        else "awaiting_review"
                    )
        record(m)
        return
    _processes.pop(m["id"], None)
    path = root / "session-outcome.json"
    outcome = (
        read_json(path)
        if path.exists()
        else {"status": "failed", "error": "Session exited without an outcome"}
    )
    if child.returncode or outcome["status"] != "completed":
        raise RuntimeError(outcome.get("error", "Session failed"))
    finish(m, "completed")


def stop(m):
    child = _processes.get(m["id"])
    if child and child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    _processes.pop(m["id"], None)
