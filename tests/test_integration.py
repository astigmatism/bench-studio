import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import router_fixture
from common import (RuntimeUnavailable, check_drift, identity, require_idle, resolve, valid_sample,
                    validate_results, write_index, atomic_json, render_index)
from worker import compatibility_probe


@pytest.fixture
def snap():
    # Contract 1.1 capabilities document (Daytime only, no offline services)
    # plus AI Runtime status for container identity.
    return router_fixture.snapshot(
        [router_fixture.model("model-a", "daytime", context=163840,
                              reasoning={"default": "default"})],
        [router_fixture.service("model-a", "container1")],
        configuration="daytime-27b",
    )


@pytest.mark.parametrize("names", [
    ["new-model"],
    ["new-model", "vendor/second:70b", "third-model"],
])
def test_discovery_offers_service_ids_and_reports_canonical_models(monkeypatch, names):
    from studio import discovery

    services = ["daytime", "nighttime", "evening"][: len(names)]
    snap = router_fixture.snapshot(
        [router_fixture.model(name, service) for name, service in zip(names, services)],
        [router_fixture.service(name, f"remote-{i}") for i, name in enumerate(names)],
    )
    monkeypatch.setattr(discovery, "fetch", lambda _: snap)
    models = discovery.discover()["models"]
    assert [m["canonical"] for m in models] == names
    # Runs start from service IDs; canonical IDs are information only.
    assert [m["alias"] for m in models] == services
    assert all(m["available"] for m in models)


def test_discovery_marks_unhealthy_remote_model_unavailable(monkeypatch):
    from studio import discovery

    snap = router_fixture.snapshot(
        [router_fixture.model("future", "daytime", available=False, context=8192)],
        [router_fixture.service("future", healthy=False)],
    )
    monkeypatch.setattr(discovery, "fetch", lambda _: snap)
    assert discovery.discover()["models"][0]["available"] is False


def test_discovery_lists_offline_services_and_router_state(monkeypatch):
    from studio import discovery

    snap = router_fixture.solo()
    monkeypatch.setattr(discovery, "fetch", lambda _: snap)
    data = discovery.discover()
    assert [m["alias"] for m in data["models"]] == ["daytime"]
    assert data["offline_services"] == [{
        "service": "nighttime", "aliases": ["nighttime"], "model": "night-a",
        "display_name": "Night-A", "reason": "exclusive_configuration",
    }]
    assert data["router"]["configuration"] == "solo-test"
    model = data["models"][0]
    assert model["configuration"] == "solo-test"
    assert model["capability_score"] == 68.3 and model["nsfw"] is False
    monkeypatch.setattr(discovery, "fetch", lambda _: router_fixture.draining(snap))
    data = discovery.discover()
    assert data["router"]["switching"] is True
    assert "switching" in data["models"][0]["error"]


def test_discovery_follows_future_runtime_model_and_context(snap):
    before = resolve(snap, "daytime")
    after = router_fixture.snapshot(
        [router_fixture.model("flash-next", "daytime", context=131072)],
        [router_fixture.service("flash-next", "container2")],
    )
    assert resolve(after, "daytime")["canonical"] == "flash-next"
    assert resolve(after, "daytime")["context"] == 131072
    with pytest.raises(RuntimeError, match="changed during"):
        check_drift(before, resolve(after, "daytime"))


def test_same_name_different_weights_invalidates_run(snap):
    before = resolve(snap, "daytime")
    after = copy.deepcopy(snap)
    after["capabilities"]["models"][0]["metadata"]["revision"] = "weights2"
    with pytest.raises(RuntimeError, match="changed during"):
        check_drift(before, resolve(after, "daytime"))


def test_processing_is_not_configuration_drift(snap):
    before = resolve(snap, "daytime")
    snap["runtime"]["services"][0]["processing"] = True
    check_drift(before, resolve(snap, "daytime"))


@pytest.mark.parametrize("change", ["active", "queued", "processing", "draining"])
def test_idle_gate_catches_existing_work(snap, change):
    if change == "processing":
        snap["runtime"]["services"][0]["processing"] = True
    else:
        key = {"active": "active_requests", "queued": "queued_requests", "draining": "draining"}[change]
        snap["runtime"]["maintenance"][key] = 1
    with pytest.raises(RuntimeError):
        require_idle(snap, ["daytime"])


def test_absent_alias_never_falls_back(snap):
    with pytest.raises(RuntimeError, match="absent"):
        resolve(snap, "nighttime")


def test_partial_metadata_is_rejected(snap):
    snap["capabilities"]["models"][0]["metadata"]["complete"] = False
    # Incomplete metadata is a failed source, not a new model: unavailable.
    with pytest.raises(RuntimeUnavailable, match="incomplete"):
        resolve(snap, "daytime")


def sample():
    return dict(ok=True, error=None, finish_reason="length", prompt_tokens=42,
                completion_tokens=16, ttft_ms=120, decode_tps=50, truncated_in_reasoning=True)


def test_reasoning_truncation_is_valid_performance_sample():
    valid_sample(sample())


@pytest.mark.parametrize("key,value", [("prompt_tokens", None), ("completion_tokens", 0),
                                       ("finish_reason", None), ("decode_tps", float("nan")),
                                       ("error", "stream failed")])
def test_invalid_samples_cannot_be_published(key, value):
    row = sample()
    row[key] = value
    with pytest.raises(RuntimeError):
        valid_sample(row)


def test_prefill_partial_success_is_not_success():
    cfg = {"run_single_stream": False, "run_prefill": True, "prefill_depths": [2000], "prefill_runs": 2}
    data = {"prefill": [{"target_depth": 2000, "prompt_tokens": [1980], "ttft_ms": [100], "pp_tps": [19800]}]}
    with pytest.raises(RuntimeError):
        validate_results(data, cfg, [])


def test_index_handles_interrupted_and_escapes_errors(tmp_path):
    atomic_json(tmp_path / "runs" / "test" / "manifest.json",
                {"id": "test", "created_at": "now", "status": "interrupted", "profile": "smoke",
                 "mode": "sequential", "requested_targets": ["daytime"], "targets": {},
                 "error": "<script>unsafe</script>"})
    write_index(tmp_path)
    text = (tmp_path / "index.html").read_text()
    assert "interrupted" in text and "&lt;script&gt;" in text
    assert "<script>unsafe" not in text


def test_stale_worker_is_unverified_without_modifying_saved_evidence(tmp_path):
    p = tmp_path / "runs" / "crashed" / "manifest.json"
    atomic_json(p, {"id": "crashed", "created_at": "2020-01-01T00:00:00+00:00",
                    "status": "running", "requested_targets": ["daytime"], "profile": "smoke",
                    "mode": "sequential", "targets": {}})
    assert "unverified" in render_index(tmp_path)
    assert json.loads(p.read_text())["status"] == "running"


@pytest.mark.parametrize("bad", [False, "no_done", "missing_usage", "error"])
def test_streaming_compatibility_requires_usage_and_clean_end(bad):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            packets = [{"choices": [{"delta": {"reasoning_content": "thinking"}, "finish_reason": None}]},
                       {"choices": [{"delta": {}, "finish_reason": "length"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2}}]
            if bad == "missing_usage": packets[-1].pop("usage")
            if bad == "error": packets.append({"error": {"message": "failed"}})
            for packet in packets:
                self.wfile.write(("data: " + json.dumps(packet) + "\n\n").encode())
            if bad != "no_done": self.wfile.write(b"data: [DONE]\n\n")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1"
        if bad:
            with pytest.raises(RuntimeError): compatibility_probe(endpoint, "mock")
        else:
            assert compatibility_probe(endpoint, "mock")["usage"]["completion_tokens"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
