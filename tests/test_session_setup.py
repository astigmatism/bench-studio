import fcntl
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from common import atomic_json
from studio import config, db, session_catalog, discovery, session_control
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
    session_control.start("test-explicit-start")
    return evidence


def test_requested_qualification_is_durable_sequential_and_not_replayed(setup_state):
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
    setup.next_check = 0
    setup.tick()
    assert len(db.runs()) == 3
    for run in runs:
        run.update(status="failed", error="Model failed qualification")
        db.update_run(run)
    setup.next_check = 0
    setup.tick()
    assert len(db.runs()) == 3
    state = json.loads((config.DATA / "session-setup.json").read_text())
    assert all(s["phase"] == "failed" for s in state["suites"].values())
    assert state["phase"] == "needs_attention"
    # Completing the control request must not hide failures behind "setup idle".
    with TestClient(app) as client:
        health = client.get("/api/health").json()["session_setup"]
    assert health["phase"] == "needs_attention"
    assert health["can_start"] and not health["can_stop"]
    assert "Setup has stopped" in health["detail"]


def test_failed_smoke_exposes_budget_and_verification_evidence(setup_state):
    SessionSetup().tick()
    run = next(r for r in db.runs() if r["profile"] == "visual-design")
    run["status"] = "completed"
    db.update_run(run)
    atomic_json(
        config.DATA / "runs" / run["id"] / "daytime/result.json",
        {
            "tasks": [
                {
                    "status": "failed",
                    "detail": "Active-time limit exhausted",
                    "active_seconds": 1800,
                    "verification_attempts": 0,
                }
            ],
        },
    )
    with TestClient(app) as client:
        suite = client.get("/api/health").json()["session_setup"]["suites"][
            "visual-design"
        ]
    assert suite["phase"] == "not_passed"
    assert "Active-time limit exhausted" in suite["detail"]
    assert "30.0 active minutes" in suite["detail"]
    assert "0 verification attempts" in suite["detail"]


def test_startup_and_legacy_auto_setting_do_not_start_checks(setup_state, monkeypatch):
    db.set_state(session_control.KEY, {})
    monkeypatch.setenv("SESSION_AUTO_SETUP", "1")
    calls = []
    monkeypatch.setattr(
        "studio.session_setup.subprocess.Popen", lambda *a, **k: calls.append(a)
    )
    monkeypatch.setattr(discovery, "discover", lambda: calls.append("discovery"))
    (config.DATA / "session-preparation.json").unlink()
    for _ in range(2):
        setup = SessionSetup()
        assert setup.tick() is False
        assert setup.tick() is False
    assert not calls and not db.runs()
    with TestClient(app) as client:
        for _ in range(2):
            state = client.get("/api/health").json()["session_setup"]
            assert state["phase"] == "paused" and state["can_start"]
            assert not state["can_stop"]


def test_start_stop_api_is_idempotent_and_only_cancels_setup_jobs(setup_state):
    db.set_state(session_control.KEY, {})
    with TestClient(app) as client:
        body = {"idempotency_key": "explicit-start-1"}
        first = client.post("/api/session-setup/start", json=body).json()
        assert client.post("/api/session-setup/start", json=body).json() == first
        assert client.get("/api/health").json()["session_setup"]["phase"] == "requested"
        setup = SessionSetup()
        setup.tick()
        assert len(db.runs()) == 3
        with db.transaction() as c:
            db.put_run(c, {"id": "user-job", "status": "queued", "created_at": "now"})
        client.post("/api/session-setup/stop", json={}).raise_for_status()
        assert all(
            r["status"] == "cancelled" for r in db.runs() if r["id"] != "user-job"
        )
        assert db.get_run("user-job")["status"] == "queued"
        assert not setup.sync_control()
        assert not SessionSetup().tick()
        assert len(db.runs()) == 4
        assert not client.post("/api/session-setup/start", json=body).json()["active"]
        second = client.post(
            "/api/session-setup/start", json={"idempotency_key": "explicit-start-2"}
        ).json()
        assert second["active"] and second["id"] != first["id"]
        SessionSetup().tick()
        assert len(db.runs()) == 7


def test_stop_preparation_releases_update_lock_and_never_resumes_on_restart(
    setup_state, monkeypatch
):
    import signal

    (config.DATA / "session-preparation.json").unlink()
    process = SimpleNamespace(pid=54321, poll=lambda: None, wait=lambda **kw: None)
    calls = []
    monkeypatch.setattr(
        "studio.session_setup.subprocess.Popen",
        lambda *a, **k: calls.append("spawn") or process,
    )
    monkeypatch.setattr("studio.session_setup.os.killpg", lambda *a: calls.append(a))
    monkeypatch.setattr(
        SessionSetup, "cleanup", lambda self, owner=None: calls.append("cleanup")
    )
    setup = SessionSetup()
    assert setup.tick()
    with TestClient(app) as client:
        client.post("/api/session-setup/stop", json={}).raise_for_status()
        state = client.get("/api/health").json()["session_setup"]
        assert state["phase"] == "stopping" and not state["can_start"]
        assert not setup.sync_control()
        state = client.get("/api/health").json()["session_setup"]
        assert state["phase"] == "paused" and state["can_start"]
    assert (54321, signal.SIGTERM) in calls and "cleanup" in calls
    assert setup.child is None
    with (config.DATA / ".execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not SessionSetup().tick()
    assert calls.count("spawn") == 1 and not db.runs()


@pytest.mark.parametrize("change_revision", [False, True])
def test_restart_and_update_require_fresh_setup_request(
    setup_state, monkeypatch, change_revision
):
    SessionSetup().tick()
    if change_revision:
        monkeypatch.setattr(config, "REVISION", "next-deployment")
    assert not SessionSetup().tick()
    assert not db.state(session_control.KEY)["active"]
    assert db.state(session_control.KEY)["outcome"] == "interrupted"
    assert len(db.runs()) == 3
    assert all(m["status"] == "cancelled" for m in db.runs())


def test_stop_marks_active_setup_for_cancellation_but_leaves_user_runs(setup_state):
    setup = SessionSetup()
    setup.tick()
    run = db.runs()[0]
    run["status"] = "running"
    db.update_run(run)
    with db.transaction() as c:
        db.put_run(c, {"id": "user-run", "status": "running", "created_at": "now"})
    session_control.stop()
    assert not setup.sync_control()
    assert db.get_run(run["id"])["cancel_requested"]
    assert not db.get_run("user-run").get("cancel_requested")
    with TestClient(app) as client:
        assert client.get("/api/health").json()["session_setup"]["phase"] == "stopping"


def test_legacy_automatic_queue_is_cancelled_during_upgrade(setup_state):
    db.set_state(session_control.KEY, {})
    with db.transaction() as c:
        db.put_run(
            c,
            {"id": "legacy-auto", "status": "queued", "created_at": "now"},
            "automatic-qualification-old",
        )
    assert not SessionSetup().tick()
    assert db.get_run("legacy-auto")["status"] == "cancelled"


def test_stop_racing_qualification_launch_cannot_leave_queued_work(
    setup_state, monkeypatch
):
    discover = discovery.discover

    def stop_during_discovery():
        session_control.stop()
        return discover()

    monkeypatch.setattr(discovery, "discover", stop_during_discovery)
    SessionSetup().tick()
    assert not db.runs()


@pytest.mark.parametrize(
    "run_status,phase,overall,detail",
    [
        ("running", "running", "qualifying", "issues-small-r1 · implementation"),
        ("queued", "queued", "waiting", "Router has existing active/queued work"),
        ("blocked", "blocked", "needs_attention", "Runtime configuration changed"),
        ("failed", "failed", "needs_attention", "Request stream interrupted"),
        ("completed", "not_passed", "needs_attention", "without passing qualification"),
    ],
)
def test_health_and_profile_read_live_progress_without_setup_tick(
    setup_state, run_status, phase, overall, detail
):
    SessionSetup().tick()
    path = config.DATA / "session-setup.json"
    original = path.read_bytes()
    run = next(r for r in db.runs() if r["profile"] == "coding-sessions")
    run.update(
        status=run_status,
        progress=detail,
        session_progress={"phase": "implementation", "turns": 12},
    )
    db.update_run(run)
    with TestClient(app) as client:
        state = client.get("/api/health").json()["session_setup"]
        profiles = client.get("/api/profiles").json()
    assert state["phase"] == overall
    suite = state["suites"]["coding-sessions"]
    assert suite["phase"] == phase and suite["turns"] == 12
    assert detail in suite["detail"]
    readiness = next(p for p in profiles if p["id"] == "coding-sessions")["preparation"]
    assert not readiness["ready"] and readiness["prepared"]
    assert detail in readiness["reason"]
    assert path.read_bytes() == original
    assert len(db.runs()) == 3


def test_qualified_suite_unlock_is_visible_while_other_suite_runs(setup_state):
    SessionSetup().tick()
    coding = next(r for r in db.runs() if r["profile"] == "coding-sessions")
    coding.update(status="completed")
    db.update_run(coding)
    setup_state["suites"]["coding-sessions"]["qualified_run"] = coding["id"]
    atomic_json(config.DATA / "session-preparation.json", setup_state)
    visual = next(r for r in db.runs() if r["profile"] == "visual-design")
    visual.update(status="running", progress="issues-small-r1 · verification")
    db.update_run(visual)
    with TestClient(app) as client:
        state = client.get("/api/health").json()["session_setup"]
    assert state["phase"] == "qualifying"
    assert "1 of 3 new suites ready" in state["detail"]
    assert state["suites"]["coding-sessions"]["phase"] == "ready"
    assert state["suites"]["visual-design"]["phase"] == "running"


def test_existing_qualified_receipt_skips_preparation_and_smoke(setup_state):
    for suite in setup_state["suites"].values():
        suite["qualified_run"] = "prior-success"
    atomic_json(config.DATA / "session-preparation.json", setup_state)
    assert SessionSetup().tick() is False
    assert not db.runs()
    with TestClient(app) as client:
        assert client.get("/api/health").json()["session_setup"]["phase"] == "ready"


def test_stale_preparation_does_not_report_ready_or_smoke_failure(setup_state):
    for suite in setup_state["suites"].values():
        suite["qualified_run"] = "prior-success"
    atomic_json(config.DATA / "session-preparation.json", setup_state)
    SessionSetup().tick()
    setup_state["source_hash"] = "changed"
    atomic_json(config.DATA / "session-preparation.json", setup_state)
    with TestClient(app) as client:
        state = client.get("/api/health").json()["session_setup"]
    assert state["phase"] == "paused"
    assert state["can_start"] and not state["can_stop"]
    assert not state["suites"]
    assert not session_catalog.readiness("coding-sessions")["ready"]


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


def test_old_updater_build_and_recreate_commands_keep_setup_manual(tmp_path):
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
    assert "SESSION_AUTO_SETUP" not in runner["environment"]
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
