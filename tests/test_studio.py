import asyncio, copy, json, importlib.util
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from common import atomic_json, now, identity, model_fingerprint
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


def test_public_models_omit_runtime_token_and_private_snapshots(client, monkeypatch):
    c, resolved, _ = client
    monkeypatch.setattr(
        "studio.discovery.discover",
        lambda: {
            "models": [{
                "alias": "model-a", "canonical": "model-a", "available": True,
                "context": 163840, "gpus": ["GPU A"], "processing": False,
                "reasoning": {}, "vision": False, "resolved": resolved,
                "identity": {"container_id": "remote-private"},
                "fingerprint": model_fingerprint(resolved),
            }],
            "runtime": {
                "ready": True, "csrf_token": "private-token",
                "services": [{"container_name": "remote-private"}],
            },
            "snapshot": {"private": "snapshot"},
        },
    )
    response = c.get("/api/models")
    assert response.status_code == 200
    body = response.json()
    assert body["runtime"] == {"ready": True}
    assert body["models"][0]["alias"] == "model-a"
    assert body["models"][0]["fingerprint"] == model_fingerprint(resolved)
    assert "private-token" not in response.text
    assert "remote-private" not in response.text
    assert "resolved" not in body["models"][0]


def test_rerun_fingerprint_detects_same_canonical_replacement(client):
    c, resolved, _ = client
    rid = launch(c).json()["id"]
    saved = c.get(f"/api/runs/{rid}").json()["model_fingerprints"]["daytime"]
    assert saved == model_fingerprint(resolved)
    replacement = copy.deepcopy(resolved)
    replacement["service"]["image_id"] = "new-image"
    assert replacement["canonical"] == resolved["canonical"]
    assert model_fingerprint(replacement) != saved


def test_new_launch_is_one_model_and_legacy_alias_remains_accepted(client, monkeypatch):
    c, resolved, _ = client
    canonical = {**resolved, "alias": "model-a", "metadata": {
        **resolved["metadata"], "aliases": ["daytime", "local-active"],
    }}
    monkeypatch.setattr(
        "studio.discovery.discover",
        lambda: {"models": [{
            "alias": "model-a", "canonical": "model-a", "available": True,
            "resolved": canonical,
        }]},
    )
    selected = launch(c, targets=["model-a"])
    assert selected.status_code == 202
    assert selected.json()["resolved"]["model-a"]["canonical"] == "model-a"
    legacy = launch(c, idempotency_key="legacy-alias-request", targets=["daytime"])
    assert legacy.status_code == 202
    assert legacy.json()["resolved"]["daytime"]["alias"] == "daytime"
    multi = launch(c, idempotency_key="new-multi-request", targets=["model-a", "daytime"])
    assert multi.status_code == 400
    assert launch(c, idempotency_key="new-parallel-request", mode="parallel").status_code == 400
    prior = legacy.json()
    prior.update(requested_targets=["daytime", "nighttime"], mode="parallel")
    db.update_run(prior)
    retry = launch(
        c, idempotency_key="legacy-alias-request",
        targets=["daytime", "nighttime"], mode="parallel",
    )
    assert retry.status_code == 202 and retry.json()["id"] == prior["id"]


def test_canonical_selection_wins_over_conflicting_legacy_alias(client, monkeypatch):
    c, resolved, _ = client
    first = {**resolved, "metadata": {
        **resolved["metadata"], "aliases": ["second-model"],
    }}
    second = {**resolved, "alias": "second-model", "canonical": "second-model"}
    monkeypatch.setattr(
        "studio.discovery.discover",
        lambda: {"models": [
            {"alias": "model-a", "canonical": "model-a", "available": True, "resolved": first},
            {"alias": "second-model", "canonical": "second-model", "available": True, "resolved": second},
        ]},
    )
    response = launch(c, targets=["second-model"])
    assert response.status_code == 202
    assert response.json()["resolved"]["second-model"]["canonical"] == "second-model"
    monkeypatch.setattr(
        "studio.discovery.discover",
        lambda: {"models": [
            {"alias": "model-a", "canonical": "model-a", "available": True, "resolved": first},
            {"alias": "second-model", "canonical": "second-model", "available": False},
        ]},
    )
    unavailable = launch(c, idempotency_key="unavailable-canonical", targets=["second-model"])
    assert unavailable.status_code == 400


def test_historical_multi_model_runs_remain_readable_when_discovery_fails(client, monkeypatch):
    c, resolved, _ = client
    old = launch(c).json()
    old["status"] = "completed"
    old["requested_targets"] = ["daytime", "nighttime"]
    old["resolved"]["nighttime"] = {
        **resolved, "alias": "nighttime", "canonical": "model-b",
    }
    db.update_run(old)
    newer = launch(c, idempotency_key="another-historical-run").json()
    newer["status"] = "completed"
    db.update_run(newer)
    for run in (old, newer):
        for target in run["requested_targets"]:
            atomic_json(
                config.DATA / "runs" / run["id"] / target / "result.json",
                {"score": 20.0, "metric": "decode_tps", "unit": "tok/s"},
            )
    monkeypatch.setattr(
        "studio.discovery.discover",
        lambda: (_ for _ in ()).throw(RuntimeError("router offline")),
    )
    history = c.get("/api/runs")
    detail = c.get(f"/api/runs/{old['id']}")
    comparison = c.get(
        "/api/compare",
        params={"a": old["id"], "b": newer["id"], "ta": "daytime", "tb": "daytime"},
    )
    assert history.status_code == detail.status_code == comparison.status_code == 200
    assert set(detail.json()["summary"]) == {"daytime", "nighttime"}
    assert detail.json()["summary"]["nighttime"]["canonical"] == "model-b"


def test_snapshot_and_queued_drift(client, monkeypatch):
    c, resolved, snap = client
    rid = launch(c).json()["id"]
    after = copy.deepcopy(resolved)
    after["context"] = 8192
    monkeypatch.setattr(runner, "snapshot", lambda _: snap)
    monkeypatch.setattr(runner, "resolve", lambda *_, **__: after)
    runner.cycle()
    assert db.get_run(rid)["status"] == "blocked"
    assert db.get_run(rid)["resolved"]["daytime"]["context"] == 163840


def test_queued_and_active_runs_detect_canonical_replacement(client, monkeypatch):
    c, resolved, snap = client
    queued = launch(c).json()
    active = launch(c, idempotency_key="active-replacement").json()
    active.update(status="running", workers={})
    db.update_run(active)
    replacement = copy.deepcopy(resolved)
    replacement["canonical"] = "model-b"
    monkeypatch.setattr(runner, "snapshot", lambda _: snap)
    monkeypatch.setattr(runner, "resolve", lambda *_, **__: replacement)
    stopped = []
    monkeypatch.setattr(runner, "stop_owned", lambda m: stopped.append(m["id"]))
    runner.cycle()
    assert db.get_run(active["id"])["status"] == "invalid"
    assert stopped == [active["id"]]
    assert db.get_run(queued["id"])["status"] == "blocked"


def test_remote_evidence_uses_only_router_and_runtime_responses(client, monkeypatch):
    c, resolved, _ = client
    canonical = resolved["canonical"]
    snap = {
        "runtime": {"services": [{
            "model": canonical, "container_name": "remote-model-container",
            "vision_device": "GPU 1",
        }]},
        "models": {"data": [{"id": canonical, "x_ollama_router": {
            "upstream_model": canonical, "capabilities": ["vision"],
            "input_modalities": ["text", "image"],
        }}]},
    }
    inspected = []
    monkeypatch.setattr(
        runner, "inspect",
        lambda name: (inspected.append(name) or {"Image": "sha256:runner"}),
    )

    class Image:
        returncode = 0
        stdout = "sha256:local-image\n"

    monkeypatch.setattr(runner, "docker", lambda *a, **k: Image())
    evidence = runner.host_evidence(snap)
    assert inspected == ["bench-studio-runner"]
    assert evidence["engine_args"][canonical] is None
    assert canonical in evidence["engine_args_unavailable"]
    assert "unavailable" in evidence["backend_defaults"][canonical]
    assert evidence["vision"][canonical]["device"] == "GPU 1"


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


def compose_config_json(root):
    data = str(root / "data")
    return json.dumps({
        "services": {
            "reports": {
                "environment": {"DATA_ROOT": "/data"},
                "user": "1000:1000",
                "volumes": [{"type": "bind", "source": data, "target": "/data"}],
            },
            "runner": {
                "environment": {
                    "DATA_ROOT": data,
                    "PROJECT_DIR": str(root),
                    "HOST_UID": "1000",
                    "HOST_GID": "1000",
                },
                "user": "1000:1000",
                "volumes": [{"type": "bind", "source": data, "target": data}],
            },
        }
    })


@pytest.mark.parametrize("restore_failure", [False, True])
def test_updater_failure_preserves_database_and_clears_maintenance(
    client, monkeypatch, tmp_path, restore_failure
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
        if args == ("docker", "compose", "config", "--format", "json"):
            return subprocess.CompletedProcess(args, 0, compose_config_json(root), "")
        if (
            args[:4] == ("docker", "compose", "--profile", "images")
            and args[-1] == "build"
        ):
            raise RuntimeError("build failed")
        return subprocess.CompletedProcess(
            args,
            1 if restore_failure and args[:3] == ("docker", "image", "tag") else 0,
            stdout="abc123",
            stderr="",
        )

    monkeypatch.setattr(mod, "run", run)
    with pytest.raises(RuntimeError, match="build failed"):
        mod.deploy()
    assert len(db.runs()) == 1
    assert (config.DATA / ".maintenance").exists() == restore_failure
    assert len([args for args in calls if args[:3] == ("docker", "image", "tag")]) == 6
    assert list((config.DATA / "backups").glob("*/studio.sqlite3"))
    assert not any("up" in args or "down" in args or "prune" in args for args in calls)


@pytest.mark.parametrize("failure", ["divergent", "unpublished", "health"])
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
        if args == ("docker", "compose", "config", "--format", "json"):
            return subprocess.CompletedProcess(args, 0, compose_config_json(root), "")
        if failure == "health" and args[:3] == ("docker", "compose", "up"):
            raise RuntimeError("health failed")
        code = (
            1
            if failure in ["divergent", "unpublished"]
            and "merge-base" in args
            and (failure == "divergent" or args[-2:] == ("HEAD", "origin/main"))
            else 0
        )
        return subprocess.CompletedProcess(args, code, stdout="abc123", stderr="")

    monkeypatch.setattr(mod, "run", run)
    with pytest.raises(RuntimeError, match=failure):
        mod.deploy()
    assert len(db.runs()) == 1
    assert (config.DATA / ".maintenance").exists() == (failure == "health")
    if failure in ["divergent", "unpublished"]:
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
    run["host"]["backend_defaults"]["daytime"] = {
        "unavailable": "Remote APIs do not expose backend defaults"
    }
    evidence = snapshot(run)["daytime"]
    assert evidence["backend_defaults_available"] is False
    assert evidence["effective_sampling"]["max_tokens"] == 2048
    assert "top_k" not in evidence["effective_sampling"]
    assert "unspecified sampling settings are unknown" in evidence["basis"]


def test_repository_preflight_rejects_unusable_verifier_before_inference(
    client, monkeypatch
):
    from studio.agent import validate_prepared_tasks

    task = {"id": "test-task", "image_id": "sha256:pinned"}
    root = config.DATA / "repository-tasks/test-task"
    root.mkdir(parents=True)
    (root / "task.toml").write_text('[environment]\ndocker_image = "sha256:pinned"\n')
    (root / "tests").mkdir()
    script = root / "tests/test.sh"
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


def test_prefill_reports_preserve_per_request_measurements_and_skips(client):
    c, _, _ = client
    m = launch(c, profile="prefill-smoke").json()
    m["status"] = "completed"
    root = config.DATA / "runs" / m["id"] / "daytime"
    root.mkdir(parents=True)
    atomic_json(
        root / "prefill.json",
        {
            "prefill": [
                {
                    "target_depth": 2000,
                    "prompt_tokens": [1970, 1972],
                    "ttft_ms": [1970, 1972],
                    "pp_tps": [1000, 1000],
                },
                {
                    "target_depth": 64000,
                    "skipped": True,
                    "reason": "Exceeds available context",
                },
            ]
        },
    )
    s = results.summarize(m)["daytime"]
    assert s["score"] == 1000 and s["count"] == 2
    assert s["tasks"][0]["prompt_tokens"] == 1970
    assert s["tasks"][1]["prefill_tps"] == 1000
    assert s["tasks"][2]["status"] == "skipped"

    atomic_json(
        root / "prefill.json",
        {
            "prefill": [
                {
                    "target_depth": 2000,
                    "prompt_tokens": [],
                    "ttft_ms": [1000],
                    "pp_tps": [1000],
                },
            ]
        },
    )
    broken = results.summarize(m)["daytime"]
    assert broken["score"] is None and broken.get("summary_error")


def test_worker_uses_captured_image_identity_instead_of_mutable_tag(
    client, monkeypatch
):
    c, _, _ = client
    m = launch(c).json()
    m["host"] = {"worker_image": "sha256:captured-image"}
    calls = []
    monkeypatch.setattr(config, "WORKER_USER", "1234:2345")
    monkeypatch.setattr(runner, "inspect", lambda name: None)
    monkeypatch.setattr(runner, "docker", lambda *a: calls.append(a))
    runner.create_worker(m, "generate", ["python", "/app/worker.py"])
    assert "sha256:captured-image" in calls[0]
    assert config.WORKER_IMAGE not in calls[0]
    assert calls[0][calls[0].index("--user") + 1] == "1234:2345"
    assert calls[1][0] == "start"


def test_host_ids_reject_non_numeric_values(monkeypatch):
    monkeypatch.setenv("HOST_UID", "42")
    assert config.host_id("HOST_UID") == "42"
    for value in ("", "1:2", "-1", "999999999999999999999"):
        monkeypatch.setenv("HOST_UID", value)
        with pytest.raises(ValueError, match="HOST_UID"):
            config.host_id("HOST_UID")


def finished_run(c, status, key):
    run = launch(c, idempotency_key=key).json()
    run["status"] = status
    db.update_run(run)
    root = config.DATA / "runs" / run["id"]
    root.mkdir(parents=True)
    atomic_json(root / "manifest.json", run)
    (root / "run.log").write_text("retained evidence")
    return run


def test_bulk_delete_removes_results_artifacts_and_references(client):
    c, _, _ = client
    removed = [finished_run(c, status, f"delete-{status}") for status in db.TERMINAL]
    kept = finished_run(c, "completed", "retained-request")
    ids = [run["id"] for run in removed]
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO baselines VALUES(?,?,?)", ("test-slot", ids[0], "daytime")
        )
        conn.execute(
            "INSERT INTO session_reviews VALUES(?,?,?,?,?,?)",
            (ids[0], "daytime", "attempt-1", 1, "{}", "review-key"),
        )
    assert c.get(f"/api/runs/{ids[0]}/export").status_code == 200
    response = c.post("/api/runs/delete", json={"ids": ids + [ids[0]]})
    assert response.status_code == 200
    assert response.json() == {"deleted": ids, "cleanup_failed": []}
    assert [r["id"] for r in c.get("/api/runs").json()] == [kept["id"]]
    for rid in ids:
        assert db.get_run(rid) is None
        assert not (config.DATA / "runs" / rid).exists()
        for suffix in ("", "/logs", "/export", "/reviews", "/artifacts/run.log"):
            assert c.get(f"/api/runs/{rid}{suffix}").status_code == 404
    assert not (config.DATA / "exports" / (ids[0] + ".zip")).exists()
    with db.connect() as conn:
        for table in ("baselines", "session_reviews", "events"):
            assert not conn.execute(
                f"SELECT 1 FROM {table} WHERE run_id=?", (ids[0],)
            ).fetchone()
    db.initialize()
    results.import_legacy()
    assert [r["id"] for r in db.runs()] == [kept["id"]]
    assert c.post("/api/runs/delete", json={"ids": ids}).status_code == 200
    assert launch(c, idempotency_key="delete-completed").status_code == 409


@pytest.mark.parametrize(
    "status", sorted(db.ACTIVE | db.WAITING | {"queued", "blocked"})
)
def test_bulk_delete_rejects_unfinished_runs_atomically(client, status):
    c, _, _ = client
    finished = finished_run(c, "failed", "finished-request")
    active = launch(c, idempotency_key="unfinished-request").json()
    active["status"] = status
    db.update_run(active)
    response = c.post("/api/runs/delete", json={"ids": [finished["id"], active["id"]]})
    assert response.status_code == 409
    assert len(db.runs()) == 2
    assert (config.DATA / "runs" / finished["id"] / "run.log").exists()


def test_bulk_delete_validates_request_and_maintenance(client):
    c, _, _ = client
    run = finished_run(c, "failed", "finished-request")
    for ids, status in (
        ([], 422),
        ([run["id"], "missing"], 404),
        ([".."], 400),
        (["../outside"], 400),
    ):
        assert c.post("/api/runs/delete", json={"ids": ids}).status_code == status
    assert (
        c.post(
            "/api/runs/delete",
            json={"ids": [run["id"]]},
            headers={"Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    (config.DATA / ".maintenance").touch()
    assert c.post("/api/runs/delete", json={"ids": [run["id"]]}).status_code == 409
    assert db.get_run(run["id"]) is not None


def test_delete_cleanup_failure_does_not_reimport_legacy_and_can_retry(
    client, monkeypatch
):
    c, _, _ = client
    run = finished_run(c, "completed", "finished-request")
    from studio import api

    with monkeypatch.context() as patch:

        def fail(*args):
            raise PermissionError("Read-only artifacts")

        patch.setattr(api.shutil, "rmtree", fail)
        response = c.post("/api/runs/delete", json={"ids": [run["id"]]})
    assert response.json()["cleanup_failed"] == [run["id"]]
    assert (config.DATA / "runs" / run["id"] / "manifest.json").exists()
    db.initialize()
    results.import_legacy()
    assert not db.runs()
    response = c.post("/api/runs/delete", json={"ids": [run["id"]]})
    assert response.json()["cleanup_failed"] == []
    assert not (config.DATA / "runs" / run["id"]).exists()


def test_delete_symlink_artifacts_never_follows_target(client, tmp_path):
    c, _, _ = client
    run = finished_run(c, "completed", "finished-request")
    outside = tmp_path / "keep"
    outside.mkdir()
    (outside / "evidence.txt").write_text("unrelated")
    (config.DATA / "runs" / run["id"] / "linked").symlink_to(
        outside, target_is_directory=True
    )
    assert c.post("/api/runs/delete", json={"ids": [run["id"]]}).status_code == 200
    assert (outside / "evidence.txt").read_text() == "unrelated"


def test_event_stream_starts_new_clients_at_end_and_replays_reconnects(
    tmp_path, monkeypatch
):
    from studio import api

    monkeypatch.setattr(config, "DATA", tmp_path)
    db.initialize()
    with db.transaction() as c:
        for n in range(3):
            db.event(c, None, {"type": "run", "n": n})

    class Connected:
        def __init__(self, headers):
            self.headers = headers

        async def is_disconnected(self):
            return False

    async def first(headers, count):
        response = await api.events(Connected(headers))
        stream = response.body_iterator
        try:
            return [await anext(stream) for _ in range(count)]
        finally:
            await stream.aclose()

    # New clients load current state themselves; old events are not replayed.
    (marker,) = asyncio.run(first({}, 1))
    assert marker.startswith("id: 3\n") and '"connected"' in marker
    replay = asyncio.run(first({"last-event-id": "1"}, 2))
    assert [chunk.split("\n")[0] for chunk in replay] == ["id: 2", "id: 3"]
