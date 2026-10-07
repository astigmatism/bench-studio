"""LLM Router client contract 1.1: offline, switching, identity, errors, events.

Runs against fixture routers only; never switches AI Runtime configurations.
"""

import asyncio
import copy
import json
import socket
import time

import pytest

import common
import router_fixture as rf
from common import (
    BenchmarkItemError,
    ModelConfigurationChanged,
    ModelOffline,
    RouterSwitching,
    RuntimeUnavailable,
    atomic_json,
    note_router_state,
    now,
    read_json,
    resolve,
    router_error,
    router_error_code,
    send_when_ready,
    wait_for_runtime,
)
from studio import config, db, runner

TASK = {"id": "task-1", "language": "python", "prompt": "def f():"}
PARAMS = {"temperature": 0, "top_p": 1, "seed": 42, "max_tokens": 64,
          "reasoning_effort": "default"}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(runner, "ROUTER", None)
    db.initialize()
    return tmp_path


def put_run(snap, target, rid, *, status="queued", family="speed", created_at=None):
    m = {
        "id": rid,
        "created_at": created_at or now(),
        "updated_at": now(),
        "status": status,
        "requested_targets": [target],
        "profile": "smoke",
        "profile_spec": {"parameters": {}},
        "family": family,
        "mode": "sequential",
        "resolved": {target: {**resolve(snap, target), "alias": target}},
        "settings": {"endpoint": "http://router/v1", "runtime_url": "http://runtime/api/status"},
        "targets": {},
        "workers": {},
        "progress": "Waiting for an idle machine",
        "revision": config.REVISION,
    }
    with db.transaction() as c:
        db.put_run(c, m)
    return m


class Deployment:
    """What runner.snapshot returns; tests swap configurations."""

    def __init__(self, snap):
        self.snap = snap

    def __call__(self, _settings):
        return copy.deepcopy(self.snap)


@pytest.fixture
def started(monkeypatch):
    calls = []

    def start(m, snap):
        calls.append(m["id"])
        m["status"] = "completed"
        db.update_run(m)

    monkeypatch.setattr(runner, "start", start)
    return calls


# --- resolve: offline, switching and identity are distinct -----------------


def test_offline_target_is_not_a_configuration_change():
    with pytest.raises(ModelOffline) as raised:
        resolve(rf.solo(), "nighttime")
    assert str(raised.value) == (
        "nighttime is offline in runtime configuration solo-test (exclusive_configuration)"
    )
    assert not isinstance(raised.value, ModelConfigurationChanged)
    assert raised.value.waiting == "waiting: nighttime offline in solo-test"
    assert raised.value.failure.startswith("model offline (solo-test): ")
    # The pinned canonical ID (legacy runs targeted it) is recognised too, and
    # the missing runtime service is not expected for a stopped model.
    with pytest.raises(ModelOffline):
        resolve(rf.solo(), "night-a", require_healthy=False)


def test_absent_target_without_offline_entry_is_a_configuration_change():
    snap = rf.solo()
    snap["capabilities"]["offline_services"] = []
    with pytest.raises(ModelConfigurationChanged, match="absent"):
        resolve(snap, "nighttime")


def test_offline_target_never_falls_back_to_daytime():
    for require_healthy in (True, False):
        with pytest.raises(ModelOffline):
            resolve(rf.solo(), "nighttime", require_healthy=require_healthy)
    assert resolve(rf.paired(), "nighttime")["canonical"] == "night-a"


def test_switching_router_defers_every_verdict():
    snap = rf.draining(rf.solo())
    for target in ("daytime", "nighttime"):
        with pytest.raises(RouterSwitching, match="switching configuration"):
            resolve(snap, target, require_healthy=False)
    maintenance = rf.paired(accepting=False, maintenance=True)
    with pytest.raises(RouterSwitching, match="maintenance"):
        resolve(maintenance, "daytime")


def test_router_facts_are_recorded_but_not_identity():
    snap = rf.paired()
    before = resolve(snap, "nighttime")
    assert before["router"]["configuration"] == "paired-test"
    assert before["router"]["capability_score"] == 64.9
    assert before["router"]["nsfw"] is True
    assert "capability_score" not in common.identity(before)
    other = rf.paired(configuration="paired-other")
    for m in other["capabilities"]["models"]:
        m["capability_score"] = 99.0
        m["nsfw"] = None
    after = resolve(other, "nighttime")
    assert common.model_fingerprint(after) == common.model_fingerprint(before)
    common.check_drift(before, after)


# --- lifecycle: queued runs wait, running runs fail, changes block ---------


def test_queued_run_waits_while_offline_and_starts_when_it_returns(state, monkeypatch, started):
    put_run(rf.paired(), "nighttime", "r-night", created_at="2026-10-07T01:00:00+00:00")
    deployment = Deployment(rf.solo())
    monkeypatch.setattr(runner, "snapshot", deployment)
    runner.cycle()
    m = db.get_run("r-night")
    assert m["status"] == "queued" and started == []
    assert m["progress"] == "waiting: nighttime offline in solo-test"
    assert m["waiting_for"]["configuration"] == "solo-test"
    # A run waiting for an offline model does not hold back later runs.
    put_run(deployment.snap, "daytime", "r-day", created_at="2026-10-07T02:00:00+00:00")
    runner.cycle()
    assert started == ["r-day"]
    assert db.get_run("r-night")["status"] == "queued"
    # Nighttime returns: the same model, in a restarted container.
    deployment.snap = rf.paired(night_container="night2")
    runner.router_changed()
    runner.cycle()
    assert started == ["r-day", "r-night"]
    m = db.get_run("r-night")
    assert "waiting_for" not in m
    assert m["resolved"]["nighttime"]["canonical"] == "night-a"
    assert m["resolved"]["nighttime"]["service"]["id"] == "night2"
    assert m["resolved_at_queue"]["nighttime"]["service"]["id"] == "night1"


@pytest.mark.parametrize("change", [{"night": "night-mtp3"}, {"night_revision": "nweights2"}])
def test_queued_run_blocks_when_target_returns_as_another_model(state, monkeypatch, started, change):
    put_run(rf.paired(), "nighttime", "r-night")
    deployment = Deployment(rf.solo())
    monkeypatch.setattr(runner, "snapshot", deployment)
    runner.cycle()
    assert db.get_run("r-night")["status"] == "queued"
    deployment.snap = rf.paired(**change)
    runner.router_changed()
    runner.cycle()
    m = db.get_run("r-night")
    assert m["status"] == "blocked" and started == []
    assert "changed" in m["progress"]


def test_running_run_fails_not_invalid_when_target_goes_offline(state, monkeypatch):
    from studio import agent

    put_run(rf.paired(), "nighttime", "r-night", status="running", family="agent")
    monkeypatch.setattr(agent, "poll_agent", lambda m: None)
    stopped = []
    monkeypatch.setattr(runner, "stop_owned", lambda m: stopped.append(m["id"]))
    deployment = Deployment(rf.draining(rf.solo()))
    monkeypatch.setattr(runner, "snapshot", deployment)
    # While the router drains (even with the new catalog published) the run waits.
    runner.cycle()
    m = db.get_run("r-night")
    assert m["status"] == "running" and "switching" in m["health_warning"]
    deployment.snap = rf.solo()
    runner.router_changed()
    runner.cycle()
    m = db.get_run("r-night")
    assert m["status"] == "failed"
    assert m["error"].startswith("model offline (solo-test): nighttime is offline")
    assert m["error_kind"] == "model_offline"
    assert stopped == ["r-night"]


@pytest.mark.parametrize("change", [{"night": "night-mtp3"}, {"night_revision": "nweights2"}])
def test_running_run_is_invalid_when_target_returns_as_another_model(state, monkeypatch, change):
    from studio import agent

    put_run(rf.paired(), "nighttime", "r-night", status="running", family="agent")
    monkeypatch.setattr(agent, "poll_agent", lambda m: None)
    monkeypatch.setattr(runner, "stop_owned", lambda m: None)
    monkeypatch.setattr(runner, "snapshot", Deployment(rf.paired(**change)))
    runner.cycle()
    assert db.get_run("r-night")["status"] == "invalid"


@pytest.mark.parametrize("kind,status", [
    ("model_offline", "failed"),
    ("configuration_changed", "invalid"),
    ("router_switching", "failed"),
    (None, "failed"),
])
def test_worker_classification_reaches_the_run(state, monkeypatch, kind, status):
    m = put_run(rf.paired(), "daytime", "r-speed", status="running")
    m["workers"] = {"bench-studio-r-speed-generate": {"role": "generate", "target": None}}
    m["stage"] = "generation"
    db.update_run(m)
    worker = {"status": "failed", "error": "model offline (solo-test): daytime is offline"}
    if kind:
        worker["error_kind"] = kind
    atomic_json(state / "runs" / "r-speed" / "manifest.json", worker)
    monkeypatch.setattr(runner, "snapshot", Deployment(rf.paired()))
    monkeypatch.setattr(runner, "stop_owned", lambda m: None)
    monkeypatch.setattr(runner, "inspect", lambda name: {
        "State": {"Status": "exited", "Running": False, "ExitCode": 1}, "Config": {"Labels": {}}})

    class Logs:
        stdout = stderr = ""

    monkeypatch.setattr(runner, "docker", lambda *a, **k: Logs())
    runner.cycle()
    m = db.get_run("r-speed")
    assert m["status"] == status
    assert m["error"] == worker["error"]


# --- router switching: wait at least ten minutes, exclude drained phases ---


def test_router_switch_waits_beyond_health_grace_without_failing(state, monkeypatch):
    m = put_run(rf.paired(), "daytime", "r1", status="running")
    deployment = Deployment(rf.draining(rf.paired()))
    monkeypatch.setattr(runner, "snapshot", deployment)
    assert runner.check_current(m) is None
    assert "switching" in m["health_warning"]
    m["switching_since"] -= 120  # beyond the 90 s health grace
    assert runner.check_current(m) is None
    deployment.snap = rf.draining(rf.paired(night="night-mtp3"))
    assert runner.check_current(m) is None  # no verdict while draining
    deployment.snap = rf.paired()
    assert runner.check_current(m) is not None
    assert "switching_since" not in m and "health_warning" not in m
    deployment.snap = rf.draining(rf.paired())
    runner.check_current(m)
    m["switching_since"] -= common.ROUTER_SWITCH_WAIT
    with pytest.raises(RuntimeUnavailable, match="did not finish switching") as raised:
        runner.check_current(m)
    assert not isinstance(raised.value, ModelConfigurationChanged)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_request_gate_waits_out_a_switch_then_rechecks_identity(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(common, "time", clock)
    expected = resolve(rf.paired(), "nighttime")
    monkeypatch.setattr(
        common, "snapshot",
        lambda _: rf.draining(rf.paired()) if clock.now < 150 else rf.paired(),
    )
    assert wait_for_runtime({}, "nighttime", expected)["canonical"] == "night-a"
    assert clock.now >= 150 and max(clock.sleeps) <= 30  # backoff 2 s .. 30 s
    clock.now = 0
    monkeypatch.setattr(
        common, "snapshot",
        lambda _: rf.draining(rf.paired()) if clock.now < 150 else rf.paired(night="night-mtp3"),
    )
    with pytest.raises(ModelConfigurationChanged):
        wait_for_runtime({}, "nighttime", expected)
    clock.now = 0
    monkeypatch.setattr(common, "snapshot", lambda _: rf.draining(rf.paired()))
    with pytest.raises(RuntimeUnavailable, match="did not finish switching"):
        wait_for_runtime({}, "nighttime", expected)
    assert clock.now >= common.ROUTER_SWITCH_WAIT
    monkeypatch.setattr(common, "snapshot", lambda _: rf.solo())
    with pytest.raises(ModelOffline):
        wait_for_runtime({}, "nighttime", expected)


def make_job(tmp_path):
    import worker

    run = tmp_path / "runs" / "r1"
    atomic_json(run / "manifest.json", {
        "id": "r1", "created_at": now(), "status": "running", "profile": "smoke",
        "mode": "sequential", "requested_targets": ["daytime"],
        "settings": {"endpoint": "http://router/v1", "runtime_url": "http://runtime/api/status"},
        "targets": {"daytime": {"status": "running", "reports": []}},
    })
    (run / "daytime").mkdir()
    return worker.Job(run / "manifest.json"), run / "daytime"


@pytest.mark.parametrize("how", ["drain-window", "switch-raised"])
def test_phase_overlapping_a_drain_is_excluded_and_measured_again(tmp_path, monkeypatch, how):
    import worker

    job, target_dir = make_job(tmp_path)
    attempts = []

    def launch():
        attempts.append(len(attempts) + 1)
        atomic_json(target_dir / "decode.json", {"attempt": len(attempts), "ttft_ms": [100]})
        if len(attempts) == 1:
            if how == "switch-raised":
                raise RouterSwitching("router switching configuration (HTTP 503 BACKEND_DRAINING)", rejected=True)
            # The runner's subscriber saw a drain begin and end mid-phase.
            note_router_state(tmp_path, False, "configuration switch")
            note_router_state(tmp_path, True)

    waited = []
    monkeypatch.setattr(worker, "wait_for_runtime", lambda settings, target, baseline: waited.append(target))
    job.measure("daytime", "decode", {"canonical": "model-a"}, launch)
    assert attempts == [1, 2] and waited == ["daytime"]
    # Only the clean attempt remains as the measurement.
    assert read_json(target_dir / "decode.json")["attempt"] == 2
    assert read_json(target_dir / "excluded" / "decode-1" / "decode.json")["attempt"] == 1
    excluded = job.manifest["targets"]["daytime"]["excluded_phases"]
    assert [e["phase"] for e in excluded] == ["decode"]
    assert "drain" in excluded[0]["reason"] or "switching" in excluded[0]["reason"]


def test_repeated_drains_fail_the_run_without_invalidating_it(tmp_path, monkeypatch):
    import worker

    job, _ = make_job(tmp_path)

    def launch():
        raise RouterSwitching("router in maintenance")

    monkeypatch.setattr(worker, "wait_for_runtime", lambda *a: None)
    with pytest.raises(RuntimeUnavailable, match="overlapped a router drain") as raised:
        job.measure("daytime", "decode", {}, launch)
    assert not isinstance(raised.value, ModelConfigurationChanged)
    assert len(job.manifest["targets"]["daytime"]["excluded_phases"]) == worker.MAX_PHASE_ATTEMPTS


def test_drain_windows_overlap_only_their_phase(tmp_path):
    note_router_state(tmp_path, False, "switch", at="2026-10-07T01:00:00+00:00")
    assert note_router_state(tmp_path, False, "switch") is False
    note_router_state(tmp_path, True, at="2026-10-07T01:02:00+00:00")
    overlap = common.drains_overlapping
    assert overlap(tmp_path, "2026-10-07T00:59:00+00:00", "2026-10-07T01:01:00+00:00")
    assert overlap(tmp_path, "2026-10-07T01:01:00+00:00", "2026-10-07T01:05:00+00:00")
    assert not overlap(tmp_path, "2026-10-07T01:03:00+00:00", "2026-10-07T01:05:00+00:00")
    assert not overlap(tmp_path, "2026-10-07T00:50:00+00:00", "2026-10-07T00:59:00+00:00")


def test_drain_rejection_is_resent_after_the_switch_and_offline_is_not(monkeypatch):
    from studio import quality_worker

    monkeypatch.setattr("time.sleep", lambda _: None)
    with rf.FixtureRouter(rf.paired()) as router:
        expected = resolve(router.snap, "daytime")
        generate = lambda: quality_worker.generate(  # noqa: E731
            router.settings["endpoint"], "model-a", TASK, PARAMS, lambda _: None,
            run_id="r1", target="daytime")
        router.responses = [(503, rf.error_body("BACKEND_DRAINING")), (200, rf.stream_body())]
        result = send_when_ready(router.settings, "daytime", expected, generate)
        assert result["finish_reason"] == "stop"
        assert len(router.headers("/v1/chat/completions")) == 2
        router.responses = [(503, rf.error_body("SERVICE_OFFLINE"))]
        with pytest.raises(ModelOffline):
            send_when_ready(router.settings, "daytime", expected, generate)
        assert len(router.headers("/v1/chat/completions")) == 3


def test_session_resends_drain_rejection_without_spending_a_turn(tmp_path, monkeypatch):
    import test_sessions as sessions
    from studio import session_catalog

    monkeypatch.setattr(config, "DATA", tmp_path)
    db.initialize()
    atomic_json(tmp_path / "session-preparation.json", {
        "protocol_version": session_catalog.PROTOCOL_VERSION,
        "source_hash": session_catalog.digest_tree(session_catalog.ROOT),
        "image_id": "sha256:fixture",
        "base_revisions": {"issue-tracker": "base1", "inventory": "base2"},
        "suites": {s: {"passed": True, "qualified_run": "qualification"}
                   for s in ("coding-sessions", "visual-design", "vision-checks")},
    })
    state = tmp_path
    rejected = RouterSwitching("router switching configuration (HTTP 503 BACKEND_DRAINING)", rejected=True)
    core, env, sent, _ = sessions.core_fixture(state, [rejected, sessions.PLAN, sessions.FINISH])
    row = asyncio.run(core.run())
    assert row["status"] == "passed"
    assert len(sent) == 3 and row["turns"] == 2
    assert len(row["requests"]) == 2  # the refused request produced nothing
    offline = ModelOffline("daytime is offline in runtime configuration solo-test",
                           target="daytime", configuration="solo-test")
    core, *_ = sessions.core_fixture(state, [offline])
    row = asyncio.run(core.run())
    assert row["status"] == "infrastructure_error" and row["error_kind"] == "model_offline"
    assert row["detail"].startswith("model offline (solo-test)")


# --- request errors are classified by error.code ---------------------------


@pytest.mark.parametrize("status,body,expected", [
    (503, rf.error_body("SERVICE_OFFLINE"), ModelOffline),
    (404, rf.error_body("MODEL_NOT_FOUND"), ModelConfigurationChanged),
    (503, rf.error_body("BACKEND_DRAINING"), RouterSwitching),
    (503, rf.error_body("MAINTENANCE_MODE"), RouterSwitching),
    (503, rf.error_body("BACKEND_UNAVAILABLE"), RuntimeUnavailable),
    (400, rf.error_body("context_length_exceeded"), BenchmarkItemError),
    (500, {"error": "legacy string error"}, RuntimeUnavailable),
    (400, rf.error_body("STATEFUL_REQUEST_UNSUPPORTED"), RuntimeError),
])
def test_error_codes_map_to_benchmark_outcomes(status, body, expected):
    error = router_error(status, body, target="nighttime")
    assert type(error) is expected
    if expected is RouterSwitching:
        assert error.rejected
    if expected is RuntimeError:
        assert not isinstance(error, RuntimeUnavailable)


def test_stream_error_codes_include_ollama_stop_reason():
    frame = {"done": True, "done_reason": "error", "error": "backend stopped",
             "x_router": {"status": "incomplete", "stop_reason": "BACKEND_UNAVAILABLE"}}
    assert router_error_code(frame) == "BACKEND_UNAVAILABLE"
    assert router_error_code({"x_router": {"status": "completed", "stop_reason": "stop"}}) is None
    drained = router_error(None, rf.error_body("BACKEND_DRAINING"), stream=True)
    assert isinstance(drained, RouterSwitching) and not drained.rejected


def _quality(router):
    from studio import quality_worker

    return quality_worker.generate(router.settings["endpoint"], "model-a", TASK, PARAMS,
                                   lambda _: None, run_id="r1", target="daytime")


def _session(router):
    from studio.router_stream import completion

    return asyncio.run(completion(router.settings["endpoint"],
                                  {"model": "model-a", "messages": []}, client_name="bench-studio/r1"))


def _probe(router):
    from worker import compatibility_probe

    return compatibility_probe(router.settings["endpoint"], "model-a", "bench-studio/r1")


def _speed(router):
    import invoke
    from betterbench.client import stream_chat_sync

    result = stream_chat_sync(router.settings["endpoint"], "model-a",
                              [{"role": "user", "content": "hi"}], max_tokens=4, temperature=0)
    failure = invoke.classify(result)
    if failure:
        raise failure
    return result


@pytest.mark.parametrize("path", [_quality, _session, _probe, _speed])
@pytest.mark.parametrize("status,code,expected", [
    (503, "SERVICE_OFFLINE", ModelOffline),
    (404, "MODEL_NOT_FOUND", ModelConfigurationChanged),
    (503, "BACKEND_DRAINING", RouterSwitching),
    (503, "BACKEND_UNAVAILABLE", RuntimeUnavailable),
    (400, "context_length_exceeded", BenchmarkItemError),
])
def test_every_request_path_classifies_router_errors(path, status, code, expected):
    with rf.FixtureRouter(rf.paired()) as router:
        router.responses = [(status, rf.error_body(code))]
        with pytest.raises(expected) as raised:
            path(router)
        if expected is RuntimeUnavailable:
            assert not isinstance(raised.value, (ModelOffline, RouterSwitching))


@pytest.mark.parametrize("path", [_quality, _session, _probe, _speed])
def test_every_request_path_rejects_an_error_inside_the_stream(path):
    frame = {"error": {"code": "BACKEND_UNAVAILABLE", "message": "backend stopped"},
             "x_router": {"status": "incomplete", "stop_reason": "BACKEND_UNAVAILABLE"}}
    body = ('data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
            "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n")
    with rf.FixtureRouter(rf.paired()) as router:
        router.responses = [(200, body)]
        with pytest.raises(RuntimeUnavailable):
            path(router)


@pytest.mark.parametrize("path", [_quality, _session, _probe, _speed])
def test_every_request_path_streams_explicitly_and_ignores_queue_keepalives(path):
    # Queued requests receive comment keepalives every 15 s before any data.
    body = ": waiting for inference slot\n\n" * 3 + rf.stream_body()
    with rf.FixtureRouter(rf.paired()) as router:
        router.responses = [(200, body)]
        path(router)
        (sent,) = router.bodies
        assert sent["stream"] is True and sent["model"] == "model-a"


def test_quality_run_records_context_rejection_as_a_failed_item(tmp_path, monkeypatch):
    from studio import quality_worker

    with rf.FixtureRouter(rf.paired()) as router:
        m = {"id": "r1", "status": "running", "mode": "sequential",
             "requested_targets": ["daytime"], "settings": router.settings,
             "resolved": {"daytime": resolve(router.snap, "daytime")},
             "profile_spec": {"parameters": PARAMS}, "targets": {}}
        atomic_json(tmp_path / "manifest.json", m)
        monkeypatch.setattr(quality_worker, "choose_tasks", lambda spec: [TASK, {**TASK, "id": "task-2"}])
        router.responses = [(400, rf.error_body("context_length_exceeded"))]
        quality_worker.main(str(tmp_path / "manifest.json"))
    rows = read_json(tmp_path / "daytime" / "responses.json")
    assert rows[0]["failure_kind"] == "context_exhausted" and rows[0]["error_code"] == "context_length_exceeded"
    assert rows[1]["finish_reason"] == "stop"
    assert read_json(tmp_path / "manifest.json")["status"] == "grading"


# --- requests identify the run; discovery identifies the application -------


def test_requests_send_client_name(monkeypatch):
    with rf.FixtureRouter(rf.paired()) as router:
        common.snapshot(router.settings)
        _quality(router)
        _session(router)
        _probe(router)
        monkeypatch.setenv("BETTERBENCH_CLIENT_NAME", "bench-studio/r1")
        _speed(router)
        assert router.headers("/v1/chat/completions") == ["bench-studio/r1"] * 4
        assert router.headers("/v1/router/capabilities") == ["bench-studio"]


# --- event stream and degraded startup -------------------------------------


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def router_events():
    with db.connect() as c:
        rows = c.execute("SELECT document FROM events ORDER BY id").fetchall()
    return [e for e in (json.loads(r["document"]) for r in rows) if e["type"] == "router_capabilities"]


def test_new_revision_records_event_and_triggers_immediate_recheck(state):
    from studio.router_events import Subscriber

    with rf.FixtureRouter(rf.paired()) as router:
        runner.CHECKED[("r1", "running")] = time.monotonic()
        runner.WAKE.clear()
        subscriber = Subscriber(router.settings, on_revision=runner.router_changed).start()
        try:
            assert wait_until(lambda: router_events() and subscriber.streaming)
            assert runner.WAKE.is_set() and not runner.CHECKED
            runner.CHECKED[("r1", "running")] = time.monotonic()
            runner.WAKE.clear()
            router.publish(rf.draining(rf.solo()))
            assert wait_until(lambda: len(router_events()) == 2)
            assert runner.WAKE.is_set() and not runner.CHECKED
            event = router_events()[-1]
            assert event["configuration"] == "solo-test"
            assert event["accepting_requests"] is False
            assert event["offline_services"] == [
                {"service": "nighttime", "model": "night-a", "reason": "exclusive_configuration"}]
            assert db.state("router")["revision"] == router.snap["capabilities"]["revision"]
            assert common.drain_windows(state)[-1]["ended_at"] is None
            router.publish(rf.solo())
            assert wait_until(lambda: len(router_events()) == 3)
            assert common.drain_windows(state)[-1]["ended_at"] is not None
        finally:
            subscriber.stop()
        assert set(router.headers("/v1/router/")) == {"bench-studio"}


def test_running_run_is_rechecked_on_new_revision_not_every_cycle(state, monkeypatch):
    from studio import agent

    put_run(rf.paired(), "daytime", "r1", status="running", family="agent")
    calls = []
    monkeypatch.setattr(runner, "check_current", lambda m: calls.append(m["id"]))
    monkeypatch.setattr(agent, "poll_agent", lambda m: None)
    runner.cycle()
    runner.cycle()
    assert calls == ["r1"]
    runner.router_changed()
    runner.cycle()
    assert calls == ["r1", "r1"]


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_runner_starts_with_the_router_unreachable(state, monkeypatch):
    port = closed_port()
    settings = {"endpoint": f"http://127.0.0.1:{port}/v1",
                "runtime_url": f"http://127.0.0.1:{port}/api/status"}
    monkeypatch.setattr(config, "SETTINGS", settings)
    subscriber = runner.start_router_watch(settings)
    try:
        assert subscriber.thread.is_alive()
        put_run(rf.paired(), "daytime", "r1")
        runner.cycle()  # degraded: the queued run keeps waiting
        m = db.get_run("r1")
        assert m["status"] == "queued" and "refused" in m["progress"].lower()
        assert subscriber.thread.is_alive()
    finally:
        subscriber.stop()


def test_reports_server_starts_with_the_router_unreachable(state, monkeypatch):
    from fastapi.testclient import TestClient
    from studio.api import app

    port = closed_port()
    monkeypatch.setattr(config, "SETTINGS", {
        "endpoint": f"http://127.0.0.1:{port}/v1",
        "runtime_url": f"http://127.0.0.1:{port}/api/status"})
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        models = client.get("/api/models")
        assert models.status_code == 503
        assert models.json()["router"] == {"reachable": False}
        assert client.get("/api/runs").status_code == 200


def test_models_api_exposes_router_state_and_offline_services(state, monkeypatch):
    from fastapi.testclient import TestClient
    from studio import discovery
    from studio.api import app

    monkeypatch.setattr(discovery, "fetch", lambda _: rf.solo())
    with TestClient(app) as client:
        body = client.get("/api/models").json()
    assert body["router"]["configuration"] == "solo-test" and not body["router"]["switching"]
    assert body["offline_services"][0]["service"] == "nighttime"
    (model,) = body["models"]
    assert model["service"] == model["alias"] == "daytime"
    assert model["configuration"] == "solo-test"
    assert model["capability_score"] == 68.3 and model["nsfw"] is False
    assert "private-token" not in json.dumps(body) and "resolved" not in model


# --- the contract travels with the project (§11) ---------------------------

CONTRACT_SHA256 = "cd9772fa3492e0c52ff090819c82a1d1f642808d136f320a43a0902917555414"


def test_contract_copy_is_verbatim_with_header_map_and_agent_rule():
    import hashlib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / "docs" / "llm-router-contract.md").read_text()
    header, rest = text.split("\n\n", 1)
    assert header.startswith("> **Vendored copy — do not edit.** LLM Router client contract, version 1.1")
    assert "`1ecc04758ad4d0a6954713defad4d02ff3e8f351`" in header
    contract, _, conformance = rest.partition("\n## How Bench Studio upholds this contract\n")
    assert hashlib.sha256(contract.encode()).hexdigest() == CONTRACT_SHA256
    for item in range(1, 12):
        assert f"| {item} |" in conformance
    assert "docs/llm-router-contract.md" in (root / "AGENTS.md").read_text()
    assert "Benchmarks never fall back" in (root / "AGENTS.md").read_text()


@pytest.mark.parametrize("context,accepted", [(8192, False), (163840, True)])
def test_launch_budgets_against_the_serving_models_context(state, monkeypatch, context, accepted):
    from fastapi.testclient import TestClient
    from studio import discovery
    from studio.api import app

    snap = rf.snapshot([rf.model("model-a", "daytime", context=context)], [rf.service("model-a")])
    monkeypatch.setattr(discovery, "fetch", lambda _: snap)
    with TestClient(app) as client:
        response = client.post("/api/runs", json={
            "targets": ["daytime"], "profile": "coding-checks", "size": "quick",
            "idempotency_key": f"context-{context}"})
    assert (response.status_code == 202) is accepted, response.text
    if accepted:
        assert response.json()["resolved"]["daytime"]["context"] == context
    else:
        assert "exceeds available context" in response.json()["detail"]
