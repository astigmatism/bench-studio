import fcntl
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from common import atomic_json
from studio import config, db, session_catalog, discovery
from studio.api import app
from studio.session_setup import SessionSetup, SUITES


@pytest.fixture
def setup_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    db.initialize()
    evidence = {
        "image_id": "sha256:prepared",
        "source_hash": session_catalog.digest_tree(session_catalog.ROOT),
        "protocol_version": session_catalog.PROTOCOL_VERSION,
        "base_revisions": {"issue-tracker": "base1", "inventory": "base2"},
        "suites": {s: {"passed": True} for s in SUITES},
    }
    atomic_json(tmp_path / "session-preparation.json", evidence)
    resolved = {
        "canonical": "model-a",
        "context": 32768,
        "reserve": 1024,
        "service": {"container_name": "daytime"},
        "metadata": {"capabilities": ["vision"], "input_modalities": ["text", "image"]},
    }
    monkeypatch.setattr(
        discovery,
        "discover",
        lambda: {
            "models": [
                {
                    "alias": "daytime",
                    "available": True,
                    "vision": True,
                    "resolved": resolved,
                }
            ]
        },
    )
    monkeypatch.setattr(
        "studio.session_setup.subprocess.check_output",
        lambda *a, **k: "sha256:prepared\n",
    )
    return evidence


def test_automatic_qualification_is_durable_sequential_and_not_replayed(setup_state):
    setup = SessionSetup()
    assert setup.tick() is False
    runs = db.runs()
    assert len(runs) == 3
    assert {r["profile"] for r in runs} == set(SUITES)
    assert all(r["mode"] == "sequential" and r["status"] == "queued" for r in runs)
    assert all(
        r["profile_spec"]["qualification"]
        and r["profile_spec"]["review_mode"] == "unattended"
        for r in runs
    )
    assert not any(session_catalog.readiness(s)["ready"] for s in SUITES)
    SessionSetup().tick()
    assert len(db.runs()) == 3
    for run in runs:
        run.update(status="failed", error="Model failed qualification")
        db.update_run(run)
    setup.next_check = 0
    setup.tick()
    assert len(db.runs()) == 3
    state = json.loads((config.DATA / "session-setup.json").read_text())
    assert all(s["phase"] == "failed" for s in state["suites"].values())


def test_existing_qualified_receipt_skips_preparation_and_smoke(setup_state):
    for suite in setup_state["suites"].values():
        suite["qualified_run"] = "prior-success"
    atomic_json(config.DATA / "session-preparation.json", setup_state)
    assert SessionSetup().tick() is False
    assert not db.runs()
    with TestClient(app) as client:
        assert client.get("/api/health").json()["session_setup"]["phase"] == "ready"


def test_discovery_outage_and_missing_vision_wait_without_enabling(
    setup_state, monkeypatch
):
    def unavailable():
        raise OSError("Router offline")

    monkeypatch.setattr(discovery, "discover", unavailable)
    setup = SessionSetup()
    setup.tick()
    assert not setup.failed and not db.runs()
    assert all(
        s["phase"] == "waiting_for_model" for s in setup.state["suites"].values()
    )
    monkeypatch.setattr(discovery, "discover", lambda: {"models": []})
    setup.next_check = 0
    setup.tick()
    assert not db.runs()


def test_preparation_reserves_update_lock_and_releases_on_failure(
    setup_state, monkeypatch
):
    (config.DATA / "session-preparation.json").unlink()
    process = SimpleNamespace(code=None, pid=2345)
    process.poll = lambda: process.code
    calls = []

    def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr("studio.session_setup.subprocess.Popen", spawn)
    monkeypatch.setattr(SessionSetup, "cleanup", lambda self, owner=None: None)
    setup = SessionSetup()
    assert setup.tick() is True
    assert "--no-build" in calls[0][0][0]
    assert calls[0][1]["env"]["STUDIO_PREPARATION_OWNER"] == setup.owner
    with (config.DATA / ".execution.lock").open("a") as lock:
        with pytest.raises(BlockingIOError):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    process.code = 1
    assert setup.tick() is False and setup.failed
    with (config.DATA / ".execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert setup.state["phase"] == "failed"
    assert "failed" in session_catalog.readiness("coding-sessions")["reason"]
    setup.tick()
    assert len(calls) == 1


def test_preparation_defers_to_update_and_paused_sessions(setup_state):
    (config.DATA / "session-preparation.json").unlink()
    (config.DATA / ".maintenance").touch()
    setup = SessionSetup()
    assert setup.tick() is False and setup.child is None
    (config.DATA / ".maintenance").unlink()
    with db.transaction() as connection:
        db.put_run(
            connection,
            {"id": "review", "created_at": "now", "status": "awaiting_review"},
        )
    setup.next_check = 0
    assert setup.tick() is False and setup.child is None


def test_changed_fixture_image_requires_preparation(setup_state, monkeypatch):
    monkeypatch.setattr(
        "studio.session_setup.subprocess.check_output",
        lambda *a, **k: "sha256:new-image\n",
    )
    calls = []
    monkeypatch.setattr(
        "studio.session_setup.subprocess.Popen",
        lambda *a, **k: calls.append(a) or SimpleNamespace(poll=lambda: None),
    )
    setup = SessionSetup()
    try:
        assert setup.tick() is True
        assert "sha256:new-image" in calls[0][0]
        assert not db.runs()
        assert not session_catalog.readiness("coding-sessions")["prepared"]
    finally:
        setup.release()


def test_interrupted_preparation_cleans_only_owned_containers(setup_state, monkeypatch):
    import signal

    calls = []
    process = SimpleNamespace(pid=4567, poll=lambda: None, wait=lambda **kwargs: None)
    monkeypatch.setattr(
        "studio.session_setup.os.killpg", lambda *args: calls.append(args)
    )

    def command(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, stdout="owned-container\n" if "ps" in args else ""
        )

    monkeypatch.setattr("studio.session_setup.subprocess.run", command)
    setup = SessionSetup()
    setup.child = process
    setup.stop()
    assert (4567, signal.SIGTERM) in calls
    assert [
        "docker",
        "ps",
        "-aq",
        "--filter",
        "label=io.bench-studio.preparation=" + setup.owner,
    ] in calls
    assert ["docker", "rm", "-f", "owned-container"] in calls
    assert setup.child is None and setup.state["phase"] == "interrupted"


def test_changed_queued_execution_image_is_rejected_before_launch(
    setup_state, monkeypatch
):
    from studio import session_runner

    old = {"task_manifest_hash": "tasks", "session_image": "old"}
    monkeypatch.setattr(
        session_runner, "attach", lambda p: {**p, "session_image": "new"}
    )
    with pytest.raises(RuntimeError, match="changed while queued"):
        session_runner.launch({"profile_spec": old})


def test_old_updater_build_and_recreate_commands_include_new_setup(tmp_path):
    """The already-installed updater reads the newly fetched Compose file."""
    project = Path(__file__).resolve().parents[1]
    env = dict(
        __import__("os").environ,
        PROJECT_DIR=str(tmp_path),
        LLM_ENDPOINT="http://runtime/v1",
        RUNTIME_URL="http://runtime/status",
    )
    if not __import__("shutil").which("docker"):
        pytest.skip("Docker Compose config validation is exercised locally")
    result = subprocess.run(
        ["docker", "compose", "--profile", "images", "config", "--format", "json"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    services = json.loads(result.stdout)["services"]
    assert (
        services["session-image"]["build"]["dockerfile"]
        == "datasets/sessions/Dockerfile"
    )
    runner = services["runner"]
    assert runner["environment"]["SESSION_AUTO_SETUP"] == "1"
    assert runner["environment"]["SESSION_SMOKE_TARGET"] == "daytime"
    assert any(v.get("target", "").endswith("/data") for v in runner["volumes"])
    labelled = [
        s
        for s in services.values()
        if s.get("labels", {}).get("io.service-portal.update.enabled") == "true"
    ]
    assert len(labelled) == 1
    assert (
        labelled[0]["labels"]["io.service-portal.update.script"]
        == "scripts/update-and-restart.sh"
    )
