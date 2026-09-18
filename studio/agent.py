"""Lifecycle of the trusted Harbor harness, separate from generated-code sandboxes."""

import os, subprocess, sys, tomllib
from pathlib import Path
from common import atomic_json, read_json
from . import config

_processes = {}


def validate_prepared_tasks(tasks):
    from .runner import docker

    for task in tasks:
        root = config.DATA / "repository-tasks" / task["id"]
        definition = tomllib.loads((root / "task.toml").read_text())
        if definition.get("environment", {}).get("docker_image") != task["image_id"]:
            raise RuntimeError("Prepared task image does not match the pinned manifest")
        scripts = list(root.rglob("*.sh"))
        if not scripts or any(not os.access(p, os.X_OK) for p in scripts):
            raise RuntimeError(
                "Repository verifier scripts are not executable; rerun task preparation"
            )
        docker("image", "inspect", task["image_id"])


def launch_agent(m):
    from .runner import log, record

    manifest = config.DATA / "repository-manifest.json"
    if not manifest.exists():
        raise RuntimeError(
            "Repository tasks have not been provisioned; run scripts/bootstrap-repositories.py"
        )
    catalog = read_json(manifest)
    count = {"quick": 2, "standard": 5, "full": 20}[m["profile_spec"]["size"]]
    eligible = catalog.get("tasks", [])
    if len(eligible) < count or not all(t.get("passed") for t in eligible[:count]):
        raise RuntimeError("Not enough oracle-validated repository tasks available")
    from .profiles import attach_manifest

    if (
        attach_manifest(dict(m["profile_spec"]))["task_manifest_hash"]
        != m["profile_spec"]["task_manifest_hash"]
    ):
        raise RuntimeError("Repository task manifest changed while queued")
    validate_prepared_tasks(eligible[:count])
    m["repository_tasks"] = eligible[:count]
    root = config.DATA / "runs" / m["id"]
    path = root / "manifest.json"
    atomic_json(path, m)
    with (root / "run.log").open("a") as output:
        child = subprocess.Popen(
            [sys.executable, "-m", "studio.agent_job", str(path)],
            stdout=output,
            stderr=subprocess.STDOUT,
            env=dict(
                os.environ,
                OPENAI_API_KEY="local-not-required",
                LITELLM_TELEMETRY="False",
                DO_NOT_TRACK="1",
            ),
            start_new_session=True,
        )
    _processes[m["id"]] = child
    m["agent_pid"] = child.pid
    record(m)
    log(m, "Harbor started; task sandboxes have no network or Docker socket")


def poll_agent(m):
    from .runner import finish, record

    child = _processes.get(m["id"])
    if child is None:
        raise RuntimeError(
            "Agent harness disappeared after controller restart; sandbox artifacts retained"
        )
    if child.poll() is None:
        root = config.DATA / "runs" / m["id"]
        count = 0
        for p in root.glob("*/harbor/*/*/result.json"):
            count += 1
        m["progress"] = (
            f'Repository tasks: {count}/{len(m["repository_tasks"])*len(m["requested_targets"])} trials finished; see Logs'
        )
        record(m)
        return
    outcome = read_json(config.DATA / "runs" / m["id"] / "agent-outcome.json")
    _processes.pop(m["id"], None)
    if child.returncode or outcome["status"] != "completed":
        raise RuntimeError(outcome.get("error", "Harbor failed"))
    finish(m, "completed")


def stop_agent(m):
    import signal

    child = _processes.get(m["id"])
    if child and child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    _processes.pop(m["id"], None)
