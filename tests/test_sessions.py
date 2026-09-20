import asyncio
import copy
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from common import atomic_json, now
from studio import config, db, profiles, results, session_catalog, session_reviews
from studio.api import app
from studio.session_core import Ledger, SessionCore
from studio.router_stream import ContextBudgetError, EmptyResponseError, completion
from studio.session_results import summarize_attempts, paired_comparison


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    db.initialize()
    evidence = {
        "protocol_version": session_catalog.PROTOCOL_VERSION,
        "source_hash": session_catalog.digest_tree(session_catalog.ROOT),
        "image_id": "sha256:fixture",
        "base_revisions": {"issue-tracker": "base1", "inventory": "base2"},
        "suites": {
            s: {"passed": True, "qualified_run": "qualification"}
            for s in ("coding-sessions", "visual-design", "vision-checks")
        },
    }
    atomic_json(tmp_path / "session-preparation.json", evidence)
    return tmp_path


def manifest(state, profile="coding-sessions", **options):
    p = profiles.attach_manifest(profiles.configure(profile, **options))
    return {
        "id": "session-test",
        "profile": profile,
        "profile_spec": p,
        "family": p["family"],
        "status": "running",
        "created_at": now(),
        "mode": "sequential",
        "requested_targets": ["daytime"],
        "resolved": {
            "daytime": {
                "canonical": "model",
                "context": 32768,
                "reserve": 1024,
                "metadata": {
                    "capabilities": {"vision": True},
                    "input_modalities": ["text", "image"],
                },
                "service": {"container_name": "model"},
            }
        },
        "settings": {"endpoint": "http://model/v1"},
    }


def put(m):
    with db.transaction() as c:
        db.put_run(c, m)


def test_tiers_and_custom_budgets(state):
    for tier, budget in session_catalog.TIERS.items():
        p = profiles.configure("coding-sessions", difficulty=tier)
        assert (p["parameters"]["task_timeout"], p["parameters"]["max_turns"]) == budget
        assert len(session_catalog.tasks(p)) == 2
    p = profiles.configure(
        "coding-sessions",
        difficulty="large",
        overrides={"task_timeout": 28800, "max_turns": 1600},
    )
    assert p["parameters"]["max_turns"] == 1600
    for overrides in ({"task_timeout": 28801}, {"max_turns": 1601}):
        with pytest.raises(ValueError):
            profiles.configure("coding-sessions", overrides=overrides)
    with pytest.raises(ValueError):
        profiles.configure("repository-tasks", overrides={"max_turns": 101})


@pytest.mark.parametrize(
    "options",
    [
        {"repetitions": 2},
        {"repetitions": True},
        {"difficulty": "huge"},
        {"difficulty": "large", "task_selection": "issues-small"},
        {"review_mode": "maybe"},
    ],
)
def test_invalid_session_options(state, options):
    with pytest.raises(ValueError):
        profiles.configure("coding-sessions", **options)


def test_preparation_gates_fixtures_not_model_success(state):
    evidence = session_catalog.receipt()
    evidence["suites"]["coding-sessions"].pop("qualified_run")
    atomic_json(state / "session-preparation.json", evidence)
    assert session_catalog.readiness("coding-sessions") == {
        "ready": True,
        "prepared": True,
        "reason": session_catalog.readiness("coding-sessions")["reason"],
    }
    p = profiles.configure("coding-sessions")
    assert profiles.attach_manifest(p)["session_image"] == "sha256:fixture"
    p["qualification"] = True
    assert profiles.attach_manifest(p)["session_image"] == "sha256:fixture"
    evidence["source_hash"] = "stale"
    atomic_json(state / "session-preparation.json", evidence)
    with pytest.raises(ValueError):
        profiles.attach_manifest(p)


def test_review_idempotency_stale_revision_and_stop(state):
    m = manifest(state)
    put(m)
    session_reviews.create(
        m["id"], "daytime", "issues-small-r1", 1, "plan", "plan.json"
    )
    a = session_reviews.decide(
        m["id"], "daytime", "issues-small-r1", 1, "revise", "Add tests", "decision-1"
    )
    assert a == session_reviews.decide(
        m["id"], "daytime", "issues-small-r1", 1, "revise", "Add tests", "decision-1"
    )
    with pytest.raises(ValueError):
        session_reviews.decide(
            m["id"], "daytime", "issues-small-r1", 1, "approve", "", "decision-2"
        )
    with pytest.raises(ValueError):
        session_reviews.decide(
            m["id"], "daytime", "issues-small-r1", 1, "approve", "", "decision-1"
        )
    session_reviews.create(
        m["id"], "daytime", "issues-small-r1", 2, "plan", "plan-2.json"
    )
    session_reviews.decide(
        m["id"], "daytime", "issues-small-r1", 2, "stop", "", "decision-3"
    )
    assert db.get_run(m["id"])["cancel_requested"]


def test_review_endpoint_survives_reconnection(state):
    m = manifest(state)
    put(m)
    session_reviews.create(
        m["id"], "daytime", "issues-small-r1", 1, "plan", "plan.json"
    )
    with TestClient(app) as client:
        assert (
            client.get("/api/runs/session-test/reviews").json()[0]["decision"] is None
        )
        url = "/api/runs/session-test/reviews/daytime/issues-small-r1/1"
        payload = {"action": "approve", "idempotency_key": "unique-decision"}
        assert client.post(url, json=payload).status_code == 200
    with TestClient(app) as client:
        assert client.post(url, json=payload).status_code == 200
        assert (
            client.get("/api/runs/session-test/reviews").json()[0]["decision"]
            == "approve"
        )


def test_ledger_excludes_review_runtime_and_setup():
    t = [0.0]
    ledger = Ledger(lambda: t[0])
    ledger.switch("preparation")
    t[0] += 20
    ledger.switch("planning")
    t[0] += 5
    ledger.switch("review_wait")
    t[0] += 300
    ledger.switch("implementation")
    ledger.implementing = True
    t[0] += 10
    ledger.switch("runtime_wait")
    t[0] += 40
    ledger.switch("compaction")
    t[0] += 3
    ledger.switch("verification")
    t[0] += 2
    ledger.switch(None)
    result = ledger.snapshot()
    assert result["active_seconds"] == 20 and result["implementation_seconds"] == 15
    assert result["phase_seconds"]["review_wait"] == 300


def test_failures_do_not_win_speed_comparison(state):
    p = profiles.configure("coding-sessions")
    rows = [
        {
            "id": "a",
            "task_id": "a",
            "status": "passed",
            "active_seconds": 40,
            "implementation_seconds": 30,
        },
        {
            "id": "b",
            "task_id": "b",
            "status": "failed",
            "active_seconds": 1,
            "implementation_seconds": 1,
        },
    ]
    a = summarize_attempts(rows, p, 2)
    assert a["score"] == 50
    assert (
        next(x for x in a["session_metrics"] if x["name"] == "implementation_seconds")[
            "value"
        ]
        == 30
    )
    b = copy.deepcopy(a)
    b["tasks"][0]["implementation_seconds"] = 15
    comparison = paired_comparison(a, b)
    assert (
        comparison["matched_count"] == 1
        and comparison["timings"][0]["improvement_percent"] == 50
    )
    b["tasks"][0]["review_history"] = [
        {"decision": "revise", "feedback": "Add keyboard support"}
    ]
    assert paired_comparison(a, b)["timings"][0]["improvement_percent"] is None
    assert summarize_attempts(rows[:1], p, 2)["score"] is None


def test_baseline_dimensions_and_legacy_slots(state):
    m = manifest(state)
    small = results.baseline_slot(m, "daytime")
    for options in (
        {"difficulty": "medium"},
        {"repetitions": 3},
        {"review_mode": "unattended"},
        {"task_selection": "issues-small"},
    ):
        assert results.baseline_slot(manifest(state, **options), "daytime") != small
    assert (
        results.baseline_slot({"profile": "coding", "family": "speed"}, "daytime")
        == "coding:standard:sequential:daytime"
    )
    other = copy.deepcopy(m)
    other["profile_spec"]["parameters"]["task_timeout"] += 1
    assert not results.comparable(m, other)


def test_context_error_is_typed_and_not_a_successful_stream():
    async def execute():
        transport = httpx.MockTransport(
            lambda r: httpx.Response(
                400,
                json={
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "Too much input",
                    }
                },
            )
        )
        with pytest.raises(ContextBudgetError):
            await completion("http://model/v1", {}, transport=transport)

    asyncio.run(execute())


class FakeEnvironment:
    def __init__(self):
        self.actions = []

    async def action(self, action, read_only):
        self.actions.append((action, read_only))
        return (
            "Planning is read-only"
            if read_only and action["action"] == "write"
            else "ok"
        )


def core_fixture(state, responses, *, interactive=False, request_override=None):
    m = manifest(state, review_mode="interactive" if interactive else "unattended")
    task = session_catalog.tasks(m["profile_spec"])[0]
    ticks = [0.0]
    env = FakeEnvironment()
    sent = []
    decisions = []

    async def request(endpoint, payload):
        sent.append(payload)
        ticks[0] += 1
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return {
            "content": json.dumps(value) if not isinstance(value, str) else value,
            "reasoning_content": "",
            "usage": {"prompt_tokens": 20, "completion_tokens": 30},
            "finish_reason": "stop",
            "ttft_ms": 2,
            "elapsed_seconds": 1,
        }

    async def review(*args):
        ticks[0] += 10
        decisions.append(args)
        return {"decision": "approve", "resume_queue_seconds": 2}

    async def ready():
        ticks[0] += 2

    async def verify(n):
        ticks[0] += 3
        return {"passed": True}

    core = SessionCore(
        manifest=m,
        target="daytime",
        task=task,
        attempt_id="issues-small-r1",
        root=state / "runs/session-test/daytime/issues-small-r1",
        environment=env,
        review=review,
        ready=ready,
        progress=lambda _: None,
        verify=verify,
        request=request_override or request,
        clock=lambda: ticks[0],
    )
    return core, env, sent, decisions


PLAN = {
    "action": "plan",
    "summary": "Add filters",
    "steps": ["Inspect", "Implement"],
    "checks": ["Regression", "Filter cases"],
}
FINISH = {"action": "finish", "commit_message": "Add issue filters"}


@pytest.mark.parametrize("style", ["router", "empty_stop", "http"])
def test_empty_generation_keeps_partial_evidence_without_executing_it(style):
    error = {"code": "EMPTY_UPSTREAM_RESPONSE", "message": "No visible answer"}
    packets = [
        {"choices": [{"delta": {"reasoning_content": "Working"}}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 10},
        },
    ]
    if style == "router":
        packets.append({"error": error})
    body = (
        "".join("data: " + json.dumps(p) + "\n\n" for p in packets) + "data: [DONE]\n\n"
    )
    transport = httpx.MockTransport(
        lambda _: httpx.Response(502, json={"error": error})
        if style == "http"
        else httpx.Response(200, text=body)
    )
    with pytest.raises(EmptyResponseError) as raised:
        asyncio.run(completion("http://model/v1", {}, transport=transport))
    evidence = raised.value.evidence
    assert evidence["content"] == ""
    assert evidence["elapsed_seconds"] >= 0
    if style != "http":
        assert evidence["usage"]["completion_tokens"] == 10
        assert evidence["reasoning_content"] == "Working"
        assert evidence["ttft_ms"] is not None
    else:
        assert evidence["usage"] is None


def test_empty_response_recovers_in_both_phases_and_counts_failed_requests(state):
    empty = EmptyResponseError(
        "EMPTY_UPSTREAM_RESPONSE", {"usage": None, "reasoning_content": "unfinished"}
    )
    core, env, sent, _ = core_fixture(state, [empty, PLAN, empty, FINISH])
    progress = []
    core.progress = progress.append
    row = asyncio.run(core.run())
    assert row["status"] == "passed" and row["verification_attempts"] == 1
    assert row["turns"] == 4 and len(row["requests"]) == 4
    assert row["active_seconds"] == 7
    assert not env.actions  # Empty generations never dispatch an action.
    assert "No action was executed" in sent[1]["messages"][-1]["content"]
    assert len(sent[1]["messages"]) > len(sent[0]["messages"])
    assert "No action was executed" in sent[3]["messages"][-1]["content"]
    saved = json.loads((core.root / "response-0001.json").read_text())
    assert saved["reasoning_content"] == "unfinished" and saved["usage"] is None
    assert not summarize_attempts([row], core.profile, 1)["usage"]["complete"]
    assert any(
        p.get("generation", {}).get("active") and p["turns"] == 1 for p in progress
    )
    assert any(p["phase"] == "planning" and p["turns"] == 2 for p in progress)


def test_repeated_empty_responses_are_bounded_failed_attempts(state):
    core, env, sent, _ = core_fixture(state, [EmptyResponseError("No answer")] * 3)
    row = asyncio.run(core.run())
    assert row["status"] == "failed" and row["failure_kind"] == "incomplete_response"
    assert "three generation attempts" in row["detail"]
    assert row["turns"] == 3 and row["active_seconds"] == 3
    assert not env.actions and row["verification_attempts"] == 0
    assert summarize_attempts([row], core.profile, 1)["score"] == 0


def test_empty_response_recovery_still_obeys_turn_budget(state):
    core, env, sent, _ = core_fixture(state, [EmptyResponseError("No answer")] * 3)
    core.params["max_turns"] = 2
    row = asyncio.run(core.run())
    assert row["failure_kind"] == "agent_budget" and row["turns"] == 2
    assert len(sent) == 2 and not env.actions


def test_empty_output_at_length_limit_is_not_retried(state):
    core, _, sent, _ = core_fixture(
        state, [EmptyResponseError("No answer", {"finish_reason": "length"})]
    )
    row = asyncio.run(core.run())
    assert row["failure_kind"] == "output_limit" and row["turns"] == 1
    assert len(sent) == 1


def test_cancellation_during_generation_is_not_retried(state):
    async def cancelled(*args):
        raise asyncio.CancelledError()

    core, _, _, _ = core_fixture(state, [], request_override=cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(core.run())
    assert core.turns == 1 and len(core.requests) == 1


def test_session_permissions_approval_and_verified_finish(state):
    core, env, sent, decisions = core_fixture(
        state,
        [
            {"action": "write", "path": "backend/app.py", "content": "bad"},
            PLAN,
            {"action": "write", "path": "frontend/src/App.tsx", "content": "feature"},
            FINISH,
        ],
        interactive=True,
    )
    row = asyncio.run(core.run())
    assert row["status"] == "passed" and row["verification_attempts"] == 1
    assert env.actions[0][1] is True and env.actions[1][1] is False
    assert len(decisions) == 1
    assert (
        row["phase_seconds"]["review_wait"] == 8
        and row["phase_seconds"]["queue_wait"] == 2
    )
    assert row["active_seconds"] == 7 and row["implementation_seconds"] == 5
    assert row["phase_seconds"]["runtime_wait"] == 8


@pytest.mark.parametrize("recovers", [True, False])
def test_visual_critique_format_recovery_is_bounded_and_preserves_feedback(
    state, recovers
):
    malformed = '{"approved":true,"findings":["No clipping"]'
    replies = (
        [malformed, {"approved": True, "findings": ["No clipping"]}]
        if recovers
        else [malformed] * 3
    )
    core, env, sent, _ = core_fixture(state, [PLAN, FINISH, *replies])
    core.visual = True
    renders = []
    verifications = []

    async def render(out):
        renders.append(out)
        return [session_catalog.ROOT / "screenshots/text-1.png"]

    async def verify(number):
        verifications.append(number)
        return {"passed": True, "prototype_artifact": "prototype.html"}

    env.render = render
    core.verify = verify
    row = asyncio.run(core.run())
    assert len(renders) == 1
    assert sent[3]["messages"][-2]["content"] == malformed
    assert "final closing brace" in sent[3]["messages"][-1]["content"]
    assert len(sent[2]["messages"]) == 3  # The first request stays immutable.
    assert row["turns"] == (4 if recovers else 5)
    assert row["active_seconds"] == row["turns"]
    assert verifications == ([1] if recovers else [])
    assert row["status"] == ("passed" if recovers else "failed")
    assert row["failure_kind"] == (None if recovers else "invalid_response")


def test_context_recovery_retains_contract_and_counts_compaction(state):
    core, env, sent, _ = core_fixture(
        state,
        [
            ContextBudgetError("full"),
            "Summary of inspected files",
            PLAN,
            ContextBudgetError("full again"),
            "Implementation progress",
            FINISH,
        ],
    )
    row = asyncio.run(core.run())
    assert row["status"] == "passed" and len(row["compactions"]) == 2
    assert all(c["success"] for c in row["compactions"])
    assert any("approved_plan" in str(payload) for payload in sent)
    assert row["phase_seconds"]["compaction"] == 2


def test_recovery_is_bounded(state):
    core, *_ = core_fixture(
        state, [ContextBudgetError("full"), "summary", ContextBudgetError("still full")]
    )
    row = asyncio.run(core.run())
    assert row["failure_kind"] == "context_limit" and row["status"] == "failed"
    assert len(row["compactions"]) == 1


def test_stop_preserves_partial_evidence(state):
    core, *_ = core_fixture(state, [PLAN], interactive=True)

    async def stop(*args):
        raise asyncio.CancelledError()

    core.review = stop
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(core.run())
    row = json.loads((core.root / "attempt.json").read_text())
    assert row["status"] == "cancelled" and row["requests"]


def test_turn_and_output_limits_are_model_outcomes(state):
    core, *_ = core_fixture(state, [PLAN, FINISH])
    core.params["max_turns"] = 1
    assert asyncio.run(core.run())["failure_kind"] == "agent_budget"

    async def length(*args, **kwargs):
        return {
            "content": "partial",
            "finish_reason": "length",
            "usage": {"prompt_tokens": 2, "completion_tokens": 8192},
        }

    core, *_ = core_fixture(state, [], request_override=length)
    assert asyncio.run(core.run())["failure_kind"] == "output_limit"


def test_database_migration_preserves_old_baselines(state):
    with db.connect() as c:
        c.execute(
            "INSERT INTO baselines VALUES('coding:standard:sequential:daytime','old-run','daytime')"
        )
        c.execute("PRAGMA user_version=1")
        c.commit()
    db.initialize()
    with db.connect() as c:
        assert c.execute("SELECT run_id FROM baselines").fetchone()[0] == "old-run"
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2


def test_prototype_csp_and_symlink_refusal(state):
    m = manifest(state)
    put(m)
    root = state / "runs" / m["id"]
    root.mkdir(parents=True)
    (root / "prototype.html").write_text('<script>fetch("/api/runs")</script>')
    (root / "linked.html").symlink_to(root / "prototype.html")
    with TestClient(app) as client:
        response = client.get("/api/runs/session-test/artifacts/prototype.html")
        assert "connect-src 'none'" in response.headers["content-security-policy"]
        assert "allow-same-origin" not in response.headers["content-security-policy"]
        assert (
            client.get("/api/runs/session-test/artifacts/linked.html").status_code
            == 404
        )


def test_waiting_session_releases_scheduler_and_resume_is_explicit(state, monkeypatch):
    from studio import runner, session_runner

    m = manifest(state)
    m["status"] = "running"
    m["revision"] = config.REVISION
    m["resolved"]["daytime"].update(
        runtime_revision="runtime", runtime_profile="profile"
    )
    put(m)
    root = state / "runs" / m["id"]
    root.mkdir(parents=True)
    key = "daytime:issues-small-r1:1"
    atomic_json(
        root / "session-progress.json",
        {
            "phase": "review_wait",
            "target": "daytime",
            "attempt_id": "issues-small-r1",
            "review_key": key,
        },
    )
    session_reviews.create(
        m["id"], "daytime", "issues-small-r1", 1, "plan", "plan.json"
    )

    class Child:
        def poll(self):
            return None

    monkeypatch.setitem(session_runner._processes, m["id"], Child())
    monkeypatch.setattr(runner, "check_current", lambda _: None)
    runner.cycle()
    assert db.get_run(m["id"])["status"] == "awaiting_review"
    session_reviews.decide(
        m["id"], "daytime", "issues-small-r1", 1, "approve", "", "approve-once"
    )
    monkeypatch.setattr(runner, "snapshot", lambda _: {})
    monkeypatch.setattr(runner, "resolve", lambda *args, **kw: m["resolved"]["daytime"])
    monkeypatch.setattr(runner, "require_idle", lambda *args, **kw: None)
    runner.cycle()
    resumed = db.get_run(m["id"])
    assert resumed["status"] == "running" and resumed["resume_review"] == key
    runner.cycle()
    assert db.get_run(m["id"])["status"] == "running"


def test_waiting_restart_interrupts_without_replay(state, monkeypatch):
    from studio import runner, session_runner

    m = manifest(state)
    m["status"] = "awaiting_review"
    put(m)
    (state / "runs" / m["id"]).mkdir(parents=True)
    monkeypatch.delitem(session_runner._processes, m["id"], raising=False)
    stopped = []
    monkeypatch.setattr(runner, "stop_owned", lambda run: stopped.append(run["id"]))
    runner.cycle()
    assert db.get_run(m["id"])["status"] == "interrupted" and stopped == [m["id"]]


def test_cancel_while_waiting_stops_owned_session(state, monkeypatch):
    from studio import runner

    m = manifest(state)
    m.update(status="awaiting_review", cancel_requested=True)
    put(m)
    (state / "runs" / m["id"]).mkdir(parents=True)
    stopped = []
    monkeypatch.setattr(runner, "stop_owned", lambda run: stopped.append(run["id"]))
    runner.cycle()
    assert db.get_run(m["id"])["status"] == "cancelled" and stopped == [m["id"]]


def test_resume_drift_invalidates_and_stops_harness(state, monkeypatch):
    from studio import runner, session_runner

    m = manifest(state)
    m.update(status="resume_queued", revision=config.REVISION)
    m["resolved"]["daytime"].update(runtime_revision="r1", runtime_profile="p")
    put(m)
    (state / "runs" / m["id"]).mkdir(parents=True)
    monkeypatch.setattr(session_runner, "poll", lambda run: None)
    monkeypatch.setattr(runner, "snapshot", lambda _: {})
    changed = copy.deepcopy(m["resolved"]["daytime"])
    changed["context"] = 65536
    monkeypatch.setattr(runner, "resolve", lambda *args, **kw: changed)
    stopped = []
    monkeypatch.setattr(runner, "stop_owned", lambda run: stopped.append(run["id"]))
    runner.cycle()
    assert db.get_run(m["id"])["status"] == "invalid" and stopped == [m["id"]]


def test_unavailable_vision_and_parallel_launch_rejected(state, monkeypatch):
    m = manifest(state, "vision-checks")
    resolved = m["resolved"]["daytime"]
    resolved["metadata"]["capabilities"]["vision"] = False
    monkeypatch.setattr(
        "studio.discovery.discover",
        lambda: {
            "models": [{"alias": "daytime", "available": True, "resolved": resolved}]
        },
    )
    with TestClient(app) as client:
        body = {
            "profile": "vision-checks",
            "targets": ["daytime"],
            "idempotency_key": "vision-unavailable",
        }
        response = client.post("/api/runs", json=body)
        assert response.status_code == 400 and "vision" in response.json()["detail"]
        body.update(
            profile="coding-sessions",
            mode="parallel",
            idempotency_key="session-parallel",
        )
        assert client.post("/api/runs", json=body).status_code == 400


@pytest.mark.parametrize(
    "failure,kind,status",
    [
        (ContextBudgetError("no image context"), "context_exhausted", "failed"),
        (TimeoutError(), "active_time_limit", "failed"),
        (RuntimeError("missing usage"), "infrastructure", "infrastructure_error"),
    ],
)
def test_vision_failure_keeps_consumed_time_and_unknown_usage(
    state, monkeypatch, failure, kind, status
):
    from studio import session_job

    async def request(*args, **kwargs):
        raise failure

    monkeypatch.setattr(session_job, "completion", request)
    m = manifest(state, "vision-checks")
    task = session_catalog.tasks(m["profile_spec"])[0]
    row = asyncio.run(session_job.vision_attempt(m, task, "vision-r1", {}, state))
    assert row["status"] == status and row["failure_kind"] == kind
    assert row["active_seconds"] >= 0 and (state / "attempt.json").exists()
    value = summarize_attempts([row], m["profile_spec"], 1)
    assert value["usage"]["prompt_tokens"] is None
    assert value["score"] == (None if status == "infrastructure_error" else 0)


def test_session_execution_image_has_separate_baseline(state):
    m = manifest(state)
    other = copy.deepcopy(m)
    other["profile_spec"]["session_image"] = "sha256:different"
    assert not results.comparable(m, other)
    assert results.baseline_slot(m, "daytime") != results.baseline_slot(
        other, "daytime"
    )


def test_live_router_capability_formats():
    assert (
        session_catalog.vision_support(
            {
                "capabilities": ["completion", "vision"],
                "input_modalities": ["text", "image"],
            }
        )
        is True
    )
    assert (
        session_catalog.vision_support(
            {"capabilities": {"vision": True}, "input_modalities": ["text", "image"]}
        )
        is True
    )
    assert (
        session_catalog.vision_support(
            {"capabilities": ["completion"], "input_modalities": ["text"]}
        )
        is False
    )
    assert session_catalog.vision_support({}) is None


def test_controller_death_cancels_inflight_session(monkeypatch):
    from studio.session_job import watch_controller
    import os

    monkeypatch.setattr(os, "getppid", lambda: 1)

    async def scenario():
        task = asyncio.create_task(asyncio.sleep(100))
        await watch_controller(task, 9999)
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_interrupted_later_attempt_keeps_its_time(state):
    m = manifest(state)
    m["status"] = "cancelled"
    root = state / "runs" / m["id"] / "daytime"
    atomic_json(root / "result.json", {"tasks": [], "score": None})
    atomic_json(
        root / "inventory-small-r1" / "attempt.json",
        {
            "id": "inventory-small-r1",
            "task_id": "inventory-small",
            "status": "cancelled",
            "active_seconds": 67,
            "implementation_seconds": 30,
            "requests": [],
        },
    )
    summary = results.summarize(m)["daytime"]
    assert summary["tasks"][0]["active_seconds"] == 67
    assert summary["score"] is None and summary["partial"]


def test_concurrent_approval_race_has_one_winner(state):
    from concurrent.futures import ThreadPoolExecutor

    m = manifest(state)
    put(m)
    session_reviews.create(
        m["id"], "daytime", "issues-small-r1", 1, "plan", "plan.json"
    )

    def decide(action):
        try:
            return session_reviews.decide(
                m["id"],
                "daytime",
                "issues-small-r1",
                1,
                action,
                "Revise plan" if action == "revise" else "",
                "race-" + action,
            )["decision"]
        except ValueError:
            return "stale"

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(decide, ["approve", "revise"]))
    assert outcomes.count("stale") == 1
    assert session_reviews.reviews(m["id"])[0]["decision"] in ("approve", "revise")


def test_harbor_command_wrapper_preserves_empty_streams(state):
    from types import SimpleNamespace
    from studio.session_environment import SessionEnvironment

    class Environment:
        async def exec(self, *args, **kwargs):
            assert kwargs["timeout_sec"] == 130
            return SimpleNamespace(
                return_code=0,
                stdout='{"exit_code":0,"stdout":"","stderr":""}',
                stderr=None,
            )

    m = manifest(state)
    env = SessionEnvironment(m, session_catalog.tasks(m["profile_spec"])[0], state)
    env.env = Environment()
    result = asyncio.run(
        env.action({"action": "exec", "command": "true"}, read_only=False)
    )
    assert json.loads(result) == {"exit_code": 0, "stdout": "", "stderr": ""}


def test_action_wrappers_continue_without_wasting_turns(state):
    core, env, sent, _ = core_fixture(
        state,
        [
            "<tool_call>\n"
            + json.dumps(PLAN)
            + "\n</parameter></function></tool_call>",
            "Run the existing checks:\n"
            + json.dumps({"action": "exec", "command": "true"}),
            "```json\n" + json.dumps(FINISH) + "\n```",
        ],
    )
    row = asyncio.run(core.run())
    assert row["status"] == "passed" and row["turns"] == 3
    assert env.actions == [({"action": "exec", "command": "true"}, False)]


@pytest.mark.parametrize(
    "value",
    [
        '[{"action":"finish"}]',
        '{"action":"finish"}{"action":"exec"}',
        '{"action":"finish"} then do something else',
        '{"action":"read","action":"exec"}',
        '<tool_call>\n{"action":"write","content":"incomplete',
        '```json\n{"action":"finish"}\n```\n{"action":"exec"}',
    ],
)
def test_action_parser_rejects_ambiguous_or_incomplete_actions(value):
    from studio.session_core import parse_json

    with pytest.raises(ValueError):
        parse_json(value)


def test_action_parser_preserves_file_contents():
    from studio.session_core import parse_json

    action = {"action": "write", "content": 'const x = {a: "</tool_call>"};\n```'}
    assert parse_json("<tool_call>\n" + json.dumps(action)) == action


def test_native_tool_parameters_are_actions_with_phase_permissions(state):
    from studio.session_core import parse_action

    text = '<tool_call>\n<function=write>\n<parameter=path>\nfrontend/test.mjs\n</parameter>\n<parameter=content>\nconst obj = {foo: "bar"};\n</parameter>\n</function>\n</tool_call>'
    assert parse_action(text) == {
        "action": "write",
        "path": "frontend/test.mjs",
        "content": '\nconst obj = {foo: "bar"};\n',
    }
    core, env, _, _ = core_fixture(state, [text, PLAN, text, FINISH])
    assert asyncio.run(core.run())["status"] == "passed"
    assert [read_only for _, read_only in env.actions] == [True, False]


@pytest.mark.parametrize(
    "value",
    [
        "<tool_call><function=exec><parameter=command>true</parameter></function>",
        "<tool_call><function=exec><parameter=command>true</parameter><parameter=command>false</parameter></function></tool_call>",
        "<tool_call><function=unknown></function></tool_call>",
        "<tool_call><function=exec></function></tool_call>",
        "<tool_call><function=exec><parameter=command>true</parameter></function></tool_call><tool_call><function=exec><parameter=command>false</parameter></function></tool_call>",
    ],
)
def test_native_tool_parser_rejects_ambiguous_or_incomplete_calls(value):
    from studio.session_core import parse_action

    with pytest.raises(ValueError):
        parse_action(value)


def test_session_execution_change_breaks_comparability(state):
    current = manifest(state)
    previous = copy.deepcopy(current)
    previous["profile_spec"]["execution_adapter_version"] = 1
    assert current["profile_spec"]["execution_adapter_version"] == 3
    assert not results.comparable(previous, current)


def test_tool_deadline_is_recoverable_but_harbor_failure_is_not(state):
    from types import SimpleNamespace
    from studio.session_environment import SessionEnvironment

    class Environment:
        async def exec(self, *args, **kwargs):
            return SimpleNamespace(
                return_code=0,
                stdout=json.dumps(
                    {
                        "exit_code": 124,
                        "timed_out": True,
                        "stdout": "partial",
                        "stderr": "",
                    }
                ),
            )

    core, _, sent, _ = core_fixture(
        state, [PLAN, {"action": "exec", "command": "sleep 999"}, FINISH]
    )
    environment = SessionEnvironment(core.manifest, core.task, state)
    environment.env = Environment()
    core.environment = environment
    assert asyncio.run(core.run())["status"] == "passed"
    assert '"timed_out": true' in sent[-1]["messages"][-1]["content"]

    class BrokenEnvironment:
        async def exec(self, *args, **kwargs):
            raise RuntimeError("Command timed out after 130 seconds")

    environment.env = BrokenEnvironment()
    with pytest.raises(RuntimeError, match="130 seconds"):
        asyncio.run(
            environment.action({"action": "exec", "command": "true"}, read_only=False)
        )

    class TerminatedSupervisor:
        async def exec(self, *args, **kwargs):
            return SimpleNamespace(return_code=143, stdout=None, stderr=None)

    environment.env = TerminatedSupervisor()
    result = json.loads(
        asyncio.run(
            environment.action({"action": "exec", "command": "true"}, read_only=False)
        )
    )
    assert result["exit_code"] == 143 and "signal" in result["detail"]


def test_preparation_preserves_concurrent_suite_qualification(state):
    incoming = session_catalog.receipt()
    incoming["suites"]["coding-sessions"].pop("qualified_run")
    session_catalog.save_preparation(incoming)
    assert (
        session_catalog.receipt()["suites"]["coding-sessions"]["qualified_run"]
        == "qualification"
    )


def test_attached_tasks_do_not_follow_catalog_edits(state, monkeypatch):
    m = manifest(state, task_selection="issues-small")
    monkeypatch.setattr(
        session_catalog, "catalog", lambda: {"tasks": [], "vision_checks": []}
    )
    assert session_catalog.tasks(m["profile_spec"])[0]["id"] == "issues-small"


def test_rendered_png_dimensions_are_actual_pixels():
    from studio.session_environment import image_dimensions

    assert image_dimensions(session_catalog.ROOT / "screenshots/text-1.png") == {
        "width": 960,
        "height": 640,
    }


def test_malformed_transport_does_not_retry_as_an_agent_action(state):
    core, env, sent, decisions = core_fixture(state, [ValueError("Malformed SSE")])
    outcome = asyncio.run(core.run())
    assert outcome["status"] == "infrastructure_error"
    assert len(sent) == 1 and outcome["turns"] == 1


def test_only_completed_smoke_is_recorded_as_a_pass(state):
    m = manifest(state, task_selection="issues-small", review_mode="unattended")
    m["profile_spec"]["qualification"] = True
    evidence = session_catalog.receipt()
    evidence["suites"]["coding-sessions"].pop("qualified_run")
    atomic_json(state / "session-preparation.json", evidence)
    atomic_json(
        state / "runs" / m["id"] / "daytime/result.json",
        {"partial": False, "passed": 1},
    )
    session_catalog.qualify_run(m)
    assert session_catalog.readiness("coding-sessions")["ready"]
    assert not session_catalog.receipt()["suites"]["coding-sessions"].get(
        "qualified_run"
    )
    m["status"] = "completed"
    session_catalog.qualify_run(m)
    assert session_catalog.readiness("coding-sessions")["ready"]


def test_graceful_controller_shutdown_preserves_session_evidence_only(
    state, monkeypatch
):
    from studio import runner

    m = manifest(state)
    m["status"] = "awaiting_review"
    put(m)
    legacy = {**m, "id": "legacy-active", "family": "speed", "status": "running"}
    put(legacy)
    stopped = []
    monkeypatch.setattr(runner, "stop_owned", lambda run: stopped.append(run["id"]))
    runner.shutdown_sessions()
    assert stopped == [m["id"]]
    assert db.get_run(m["id"])["status"] == "interrupted"
    assert db.get_run("legacy-active")["status"] == "running"
