import copy, json, importlib.util
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from common import atomic_json, now, identity
from studio import config, db, profiles, results, runner
from studio.api import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    resolved = {
        "alias": "daytime",
        "canonical": "model-a",
        "context": 163840,
        "reserve": 1024,
        "runtime_revision": "r1",
        "runtime_profile": "day",
        "metadata": {
            "revision": "m1",
            "quantization": "Q8",
            "reasoning": {"efforts": {"off": "none", "low": "low"}},
        },
        "service": {
            "id": "c1",
            "image_id": "i1",
            "container_name": "day",
            "gpu_names": ["GPU A"],
            "model": "model-a",
        },
    }
    snap = {
        "models": {"data": []},
        "runtime": {"ready": True, "maintenance": {}, "services": []},
    }
    monkeypatch.setattr(
        "studio.discovery.discover",
        lambda: {
            "models": [{"alias": "daytime", "available": True, "resolved": resolved}],
            "runtime": snap["runtime"],
            "snapshot": snap,
        },
    )
    with TestClient(app) as c:
        yield c, resolved, snap


def launch(c, **kw):
    return c.post(
        "/api/runs",
        json={
            "targets": ["daytime"],
            "profile": "smoke",
            "idempotency_key": "test-request-0001",
            **kw,
        },
    )


def test_durable_idempotent_queue_and_cancel(client):
    c, _, _ = client
    a = launch(c)
    assert a.status_code == 202
    b = launch(c)
    assert a.json()["id"] == b.json()["id"]
    assert len(db.runs()) == 1
    rid = a.json()["id"]
    db.initialize()
    assert db.get_run(rid)["status"] == "queued"
    assert c.post(f"/api/runs/{rid}/cancel", json={}).json()["status"] == "cancelled"
    assert (
        c.post(f"/api/runs/{rid}/baseline", json={"target": "daytime"}).status_code
        == 400
    )


def test_profile_validation_and_immutable_custom(client):
    c, _, _ = client
    assert launch(c, overrides={"top_k": 20}).status_code == 400
    assert launch(c, overrides={"temperature": -1}).status_code == 400
    assert launch(c, overrides={"max_tokens": 99999999}).status_code == 400
    before = profiles.get("coding")
    custom = c.post(
        "/api/profiles",
        json={
            "profile": "coding",
            "name": "Cold baseline",
            "overrides": {"temperature": 0},
        },
    ).json()
    assert custom["parameters"]["temperature"] == 0 and not custom["builtin"]
    assert profiles.get("coding") == before


def test_csrf_and_maintenance(client):
    c, _, _ = client
    assert (
        c.post(
            "/api/runs", json={}, headers={"Origin": "https://attacker.invalid"}
        ).status_code
        == 403
    )
    assert c.post("/api/runs", data="{}").status_code == 415
    (config.DATA / ".maintenance").touch()
    assert launch(c).status_code == 409


def test_snapshot_and_queued_drift(client, monkeypatch):
    c, resolved, snap = client
    rid = launch(c).json()["id"]
    after = copy.deepcopy(resolved)
    after["context"] = 8192
    monkeypatch.setattr(runner, "snapshot", lambda _: snap)
    monkeypatch.setattr(runner, "resolve", lambda *_: after)
    runner.cycle()
    assert db.get_run(rid)["status"] == "blocked"
    assert db.get_run(rid)["resolved"]["daytime"]["context"] == 163840


def test_busy_queue_does_not_launch(client, monkeypatch):
    c, resolved, snap = client
    rid = launch(c).json()["id"]
    monkeypatch.setattr(runner, "snapshot", lambda _: snap)
    monkeypatch.setattr(runner, "resolve", lambda *_: resolved)

    def busy(*args):
        raise RuntimeError("Backend busy")

    monkeypatch.setattr(runner, "require_idle", busy)
    monkeypatch.setattr(
        runner, "start", lambda *_: pytest.fail("Must not start busy benchmark")
    )
    runner.cycle()
    assert db.get_run(rid)["status"] == "queued"
    assert "busy" in db.get_run(rid)["progress"]


def test_artifact_escape_rejected(client, tmp_path):
    c, _, _ = client
    rid = launch(c).json()["id"]
    directory = config.DATA / "runs" / rid
    directory.mkdir(parents=True)
    (directory / "good.txt").write_text("hello")
    (directory / "escape.txt").symlink_to("/etc/passwd")
    assert c.get(f"/api/runs/{rid}/artifacts/good.txt").text == "hello"
    assert c.get(f"/api/runs/{rid}/artifacts/escape.txt").status_code == 404


def test_legacy_import_and_real_weighted_summary(client):
    c, resolved, _ = client
    rid = "legacy-result"
    root = config.DATA / "runs" / rid / "daytime"
    root.mkdir(parents=True)
    m = {
        "id": rid,
        "created_at": now(),
        "status": "completed",
        "profile": "smoke",
        "mode": "sequential",
        "requested_targets": ["daytime"],
        "resolved": {"daytime": resolved},
    }
    atomic_json(root.parent / "manifest.json", m)
    rows = [
        {
            "ok": True,
            "decode_tps": 20,
            "ttft_ms": 100,
            "completion_tokens": 100,
            "n_chunks": 100,
            "itl_ms": [50] * 99,
        }
        for _ in range(5)
    ]
    atomic_json(
        root / "decode.json",
        {"single_stream": {"code": rows}, "config": {"weights": {"code": 1}}},
    )
    results.import_legacy()
    results.import_legacy()
    stored = db.get_run(rid)
    assert results.summarize(stored)["daytime"]["score"] == 20
    stored["status"] = "interrupted"
    assert results.summarize(stored)["daytime"]["score"] is None
    assert len(db.runs()) == 1


def test_incompatible_comparison(client):
    c, _, _ = client
    rid = launch(c).json()["id"]
    a = db.get_run(rid)
    b = copy.deepcopy(a)
    a["status"] = b["status"] = "completed"
    b["profile"] = "prefill"
    with pytest.raises(ValueError):
        results.compare(a, b, "daytime", "daytime")


def test_cancel_ownership_guard(client, monkeypatch):
    c, _, _ = client
    m = launch(c).json()
    m["workers"] = {"some-other-app": {}}
    calls = []
    monkeypatch.setattr(
        runner,
        "inspect",
        lambda _: {"Config": {"Labels": {}}, "State": {"Running": True}},
    )

    class R:
        stdout = ""

    monkeypatch.setattr(
        runner, "docker", lambda *args, **kw: (calls.append(args) or R())
    )
    runner.stop_owned(m)
    assert not any(x[0] == "stop" for x in calls)


def test_updater_rejects_dirty_before_network(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "updater", Path(__file__).parents[1] / "scripts/update.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class P:
        stdout = " M frontend/src/main.tsx"
        returncode = 0

    calls = []
    monkeypatch.setattr(mod.shutil, "which", lambda x: "/bin/" + x)
    monkeypatch.setattr(mod, "run", lambda *a, **k: (calls.append(a) or P()))
    with pytest.raises(RuntimeError, match="dirty"):
        mod.preflight()
    assert calls == [("git", "status", "--porcelain")]


def test_idempotency_survives_discovery_outage(client, monkeypatch):
    c, _, _ = client
    original = launch(c).json()
    monkeypatch.setattr(
        "studio.discovery.discover",
        lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    assert launch(c).json()["id"] == original["id"]


def test_disappeared_worker_is_interrupted_without_replay(client, monkeypatch):
    c, _, _ = client
    m = launch(c).json()
    m.update(status="running", workers={"owned-worker": {"role": "generate"}})
    db.update_run(m)
    monkeypatch.setattr(runner, "check_current", lambda m: None)
    monkeypatch.setattr(runner, "inspect", lambda n: None)
    monkeypatch.setattr(runner, "stop_owned", lambda m: None)
    monkeypatch.setattr(
        runner,
        "create_worker",
        lambda *a, **k: pytest.fail("Must never replay disappeared work"),
    )
    runner.cycle()
    assert db.get_run(m["id"])["status"] == "interrupted"


def test_running_worker_is_reconciled_without_replay(client, monkeypatch):
    c, _, _ = client
    m = launch(c).json()
    m.update(status="running", workers={"owned-worker": {"role": "generate"}})
    db.update_run(m)
    monkeypatch.setattr(runner, "check_current", lambda m: None)
    monkeypatch.setattr(
        runner, "inspect", lambda n: {"State": {"Status": "running", "Running": True}}
    )
    monkeypatch.setattr(
        runner,
        "create_worker",
        lambda *a, **k: pytest.fail("Must not replay surviving worker"),
    )
    runner.cycle()
    assert db.get_run(m["id"])["status"] == "running"


def test_quality_requires_output_budget_and_prefill_temperature_is_fixed(client):
    c, _, _ = client
    assert (
        launch(
            c, profile="coding-checks", size="quick", overrides={"max_tokens": None}
        ).status_code
        == 400
    )
    assert (
        launch(c, profile="prefill", overrides={"temperature": 0.7}).status_code == 400
    )


def test_arbitrary_provider_names_are_safe_and_resolve():
    from common import safe_target, resolve

    name = "vendor/../model:70b"
    target = safe_target(name)
    assert "/" not in target and ":" not in target
    snap = {
        "runtime": {"services": [{"model": name, "healthy": True, "running": True}]},
        "models": {
            "data": [
                {
                    "id": name,
                    "x_ollama_router": {
                        "complete": True,
                        "health": {"available": True},
                        "context_window": 8192,
                    },
                }
            ]
        },
    }
    assert resolve(snap, target)["canonical"] == name


def test_updater_failure_preserves_database_and_clears_maintenance(
    client, monkeypatch, tmp_path
):
    import subprocess, sqlite3

    c, _, _ = client
    launch(c)
    spec = importlib.util.spec_from_file_location(
        "updater_failure", Path(__file__).parents[1] / "scripts/update.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    root = tmp_path / "deployment"
    root.mkdir()
    (root / "data").symlink_to(config.DATA, target_is_directory=True)
    monkeypatch.setattr(mod, "ROOT", root)
    monkeypatch.setattr(mod, "preflight", lambda: None)
    calls = []

    def run(*args, **kw):
        calls.append(args)
        if (
            args[:4] == ("docker", "compose", "--profile", "images")
            and args[-1] == "build"
        ):
            raise RuntimeError("build failed")
        return subprocess.CompletedProcess(args, 0, stdout="abc123", stderr="")

    monkeypatch.setattr(mod, "run", run)
    with pytest.raises(RuntimeError, match="build failed"):
        mod.deploy()
    assert len(db.runs()) == 1 and not (config.DATA / ".maintenance").exists()
    assert list((config.DATA / "backups").glob("*/studio.sqlite3"))
    assert not any("up" in args or "down" in args or "prune" in args for args in calls)


@pytest.mark.parametrize("failure", ["divergent", "health"])
def test_updater_rejects_divergence_and_failed_health(
    client, monkeypatch, tmp_path, failure
):
    import subprocess

    c, _, _ = client
    launch(c)
    spec = importlib.util.spec_from_file_location(
        "updater_" + failure, Path(__file__).parents[1] / "scripts/update.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    root = tmp_path / "deployment"
    root.mkdir()
    (root / "data").symlink_to(config.DATA, target_is_directory=True)
    monkeypatch.setattr(mod, "ROOT", root)
    monkeypatch.setattr(mod, "preflight", lambda: None)
    calls = []

    def run(*args, **kw):
        calls.append(args)
        if failure == "health" and args[:3] == ("docker", "compose", "up"):
            raise RuntimeError("health failed")
        code = 1 if failure == "divergent" and "merge-base" in args else 0
        return subprocess.CompletedProcess(args, code, stdout="abc123", stderr="")

    monkeypatch.setattr(mod, "run", run)
    with pytest.raises(RuntimeError, match=failure):
        mod.deploy()
    assert len(db.runs()) == 1 and not (config.DATA / ".maintenance").exists()
    if failure == "divergent":
        assert not any("build" in args for args in calls)
    assert not any("down" in args or "prune" in args for args in calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("branch", "feature"),
        ("origin", "https://example.invalid/repo"),
        ("upstream", "other/main"),
    ],
)
def test_updater_rejects_unexpected_source(monkeypatch, field, value):
    import subprocess

    spec = importlib.util.spec_from_file_location(
        "updater_source", Path(__file__).parents[1] / "scripts/update.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.shutil, "which", lambda t: "/bin/" + t)

    def run(*args, **kw):
        output = ""
        if "symbolic-ref" in args:
            output = value if field == "branch" else "main"
        if "get-url" in args:
            output = (
                value
                if field == "origin"
                else "https://github.com/astigmatism/bench-studio.git"
            )
        if "@{upstream}" in args:
            output = value if field == "upstream" else "origin/main"
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(mod, "run", run)
    with pytest.raises(RuntimeError, match="Refusing"):
        mod.preflight()


@pytest.mark.parametrize(
    "stderr", ["Error: No such object: worker", "error: no such object: worker"]
)
def test_docker_missing_worker_error_is_case_insensitive(monkeypatch, stderr):
    import subprocess

    monkeypatch.setattr(
        runner,
        "docker",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "[]", stderr),
    )
    assert runner.inspect("worker") is None


@pytest.mark.parametrize("fault", [None, "usage", "finish", "done", "json"])
def test_harbor_stream_adapter_requires_complete_real_measurements(fault):
    import asyncio, httpx
    from studio.router_stream import completion

    chunks = [
        {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
        {
            "choices": [
                {"delta": {}, "finish_reason": None if fault == "finish" else "stop"}
            ],
            "usage": (
                None
                if fault == "usage"
                else {"prompt_tokens": 10, "completion_tokens": 2}
            ),
        },
    ]
    text = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
    if fault != "done":
        text += "data: [DONE]\n\n"
    if fault == "json":
        text = "data: broken\n\n" + text

    def handler(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200, text=text, headers={"content-type": "text/event-stream"}
        )

    call = lambda: asyncio.run(
        completion(
            "http://router/v1",
            {"model": "test"},
            transport=httpx.MockTransport(handler),
        )
    )
    if fault:
        with pytest.raises((RuntimeError, ValueError)):
            call()
    else:
        assert call()["content"] == "hello"


def test_generation_records_defaults_overrides_and_native_workload_budgets(client):
    from studio.generation import snapshot

    profile = profiles.configure("smoke")
    run = {
        "profile_spec": profile,
        "requested_targets": ["daytime"],
        "resolved": {"daytime": {"metadata": {"reasoning": {"default": "default"}}}},
        "host": {
            "backend_defaults": {"daytime": {"params": {"temperature": 1, "top_k": 20}}}
        },
    }
    evidence = snapshot(run)["daytime"]
    assert evidence["effective_sampling"]["temperature"] == 0.7
    assert evidence["effective_sampling"]["top_k"] == 20
    assert "top_k" not in evidence["request_overrides"]
    assert "max_tokens" not in evidence["request_overrides"]
    assert evidence["output_budgets_by_workload"]
    assert evidence["reasoning"] == "default"
    run["profile_spec"] = profiles.configure(
        "coding-checks", "quick", {"reasoning_effort": "off", "max_tokens": 2048}
    )
    evidence = snapshot(run)["daytime"]
    assert evidence["request_overrides"]["max_tokens"] == 2048
    assert evidence["reasoning"] == "none"
    assert evidence["output_budgets_by_workload"] == {}


def test_repository_preflight_rejects_unusable_verifier_before_inference(
    client, monkeypatch
):
    from studio.agent import validate_prepared_tasks

    task = {"id": "test-task", "image_id": "sha256:pinned"}
    root = config.DATA / "repository-tasks/test-task"
    root.mkdir(parents=True)
    (root / "task.toml").write_text('[environment]\ndocker_image = "sha256:pinned"\n')
    script = root / "test.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o644)
    calls = []
    monkeypatch.setattr(runner, "docker", lambda *a: calls.append(a))
    with pytest.raises(RuntimeError, match="not executable"):
        validate_prepared_tasks([task])
    assert not calls
    script.chmod(0o755)
    validate_prepared_tasks([task])
    assert calls == [("image", "inspect", "sha256:pinned")]
    (root / "task.toml").write_text('[environment]\ndocker_image = "different"\n')
    with pytest.raises(RuntimeError, match="pinned manifest"):
        validate_prepared_tasks([task])
