import asyncio
import copy
import json

import httpx
import pytest

from betterbench.telemetry import StreamTelemetry
from common import atomic_json
from studio import config, db, performance, results
from studio.router_stream import completion


def request(tokens=101, seconds=3, ttft=1000, **extra):
    return dict(
        usage={"completion_tokens": tokens, "prompt_tokens": 1000},
        elapsed_seconds=seconds,
        ttft_ms=ttft,
        finish_reason="stop",
        **extra,
    )


def metric(summary, key="output_tps"):
    return next(m for m in summary["performance"]["metrics"] if m["id"] == key)


def summary(requests=None):
    s = {
        "canonical": "model-a",
        "score": 100,
        "unit": "%",
        "count": 1,
        "passed": 1,
        "metric": "commit_ready_rate",
        "tasks": [
            {"id": "task-r1", "status": "passed", "requests": requests or [request()]}
        ],
    }
    performance.attach(s)
    return s


def run(rid, **extra):
    return {
        "id": rid,
        "status": "completed",
        "created_at": rid,
        "family": "session",
        "profile": "sessions",
        "mode": "sequential",
        "requested_targets": ["day"],
        "profile_spec": {
            "suite": "coding-sessions",
            "task_manifest_hash": "tasks1",
            "size": "standard",
            "engine": "test",
            "parameters": {"task_timeout": 10},
        },
        "resolved": {"day": {"canonical": "model-a"}},
        **extra,
    }


def test_measurements_exclude_request_errors_but_not_task_failure():
    s = summary(
        [
            request(),
            request(tokens=51, seconds=2),
            request(error="recovered"),
            request(tokens=1),
            request(tokens=0),
            request(tokens=None),
        ]
    )
    assert metric(s)["value"] == 50
    assert metric(s)["samples"] == 2
    assert metric(s)["total"] == 6
    assert metric(s, "request_seconds")["samples"] == 5
    assert metric(s, "prompt_tps")["value"] is None
    assert s["performance"]["request_errors"] == 1
    s["tasks"][0]["status"] = "failed"
    performance.attach(s)
    assert metric(s)["value"] == 50


@pytest.mark.parametrize(
    "changes",
    [
        {"elapsed_seconds": 1},
        {"elapsed_seconds": 0},
        {"elapsed_seconds": -1},
        {"elapsed_seconds": float("nan")},
        {"ttft_ms": None},
        {"ttft_ms": -10},
        {"ttft_ms": 5000},
        {"finish_reason": None},
        {"finish_reason": "error"},
        {"usage": {}},
        {"usage": {"completion_tokens": True}},
        {"ok": False},
    ],
)
def test_invalid_speed_is_missing(changes):
    q = request()
    q.update(changes)
    assert "output_tps" not in performance.request_values(q)


def test_truncation_is_completed_generation_and_native_prefill_excludes_cache():
    q = request()
    q.update(
        finish_reason="length",
        timings={
            "prompt_n": 100,
            "prompt_ms": 200,
            "cache_n": 900,
            "predicted_n": 101,
            "predicted_ms": 1000,
        },
    )
    values = performance.request_values(q)
    assert values["prompt_tps"] == 500
    assert values["prompt_estimate_tps"] == 1000
    assert values["native_output_tps"] == 101
    assert values["output_tps"] == 50
    assert values["tpot_ms"] == 20


def test_tail_threshold_and_update_gaps_not_tokens():
    s = summary([request(update_gaps_ms=[20, 40])] * 99)
    assert metric(s)["distribution"]["tail"] is None
    assert metric(s, "update_gap_ms")["samples"] == 198
    assert metric(s, "update_gap_ms")["coverage_unit"] == "stream gaps"
    s = summary([request()] * 100)
    assert metric(s)["distribution"]["tail"] == 50


def test_telemetry_captures_reasoning_answer_and_optional_timings():
    t = StreamTelemetry(10)
    t.observe({"choices": [{"delta": {"reasoning_content": "thinking"}}]}, 11)
    t.observe({"choices": [{"delta": {"content": "several tokens"}}]}, 12)
    t.observe({"timings": {"prompt_n": 10, "prompt_ms": 50, "cache_n": 100}}, 13)
    e = t.evidence(14)
    assert e["ttft_ms"] == 1000 and e["ttfa_ms"] == 2000
    assert e["last_output_ms"] == 2000 and e["completion_ms"] == 4000
    assert e["update_gaps_ms"] == [1000] and e["n_updates"] == 2
    assert e["timings"]["cache_n"] == 100
    t.observe({"prompt_eval_duration": 100_000_000, "prompt_eval_count": 20}, 14)
    assert t.evidence(15)["timings"]["prompt_ms"] == 100


def test_stream_retains_timing_on_incomplete_response():
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
        {"timings": {"prompt_n": 100, "prompt_ms": 50}},
    ]
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
        )
    )
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(completion("http://model/v1", {}, transport=transport))
    assert exc.value.evidence["timings"]["prompt_n"] == 100
    assert exc.value.evidence["ttft_ms"] is not None


def test_canonical_model_ranks_previous_run_direction_ties_and_conditions():
    runs = [run(str(i)) for i in range(1, 5)]
    summaries = {
        str(i): {"day": summary([request(tokens=t)])}
        for i, t in enumerate([101, 121, 121, 161], 1)
    }
    summaries["4"]["day"]["canonical"] = "model-b"
    runs[1]["profile_spec"]["parameters"]["temperature"] = 0.5
    performance.rank_history(runs, summaries)
    m = metric(summaries["2"]["day"])
    assert m["model_rank"] == {"rank": 1, "total": 3, "tied": True}
    assert m["overall_rank"]["rank"] == 2 and m["overall_rank"]["total"] == 4
    assert m["previous"]["run_id"] == "1"
    assert m["previous"]["improvement"] == 20
    assert m["previous"]["differences"] == [
        {"field": "temperature", "a": None, "b": 0.5}
    ]
    assert metric(summaries["3"]["day"])["previous"]["run_id"] == "2"
    assert metric(summaries["1"]["day"])["previous"] is None
    assert metric(summaries["4"]["day"])["model_rank"]["total"] == 1
    # Lower latency is a gain, not a regression.
    summaries["2"]["day"] = summary([request(seconds=2)])
    performance.rank_history(runs, summaries)
    assert metric(summaries["2"]["day"], "request_seconds")["previous"][
        "improvement"
    ] == pytest.approx(100 / 3)


def test_matching_workload_ignores_profile_copy_and_tuning_only():
    a = run("1")
    b = copy.deepcopy(a)
    b["profile"] = "custom-1"
    b["profile_spec"].update(id="custom-1", name="Fast", parent="sessions")
    b["profile_spec"]["parameters"].update(temperature=0.5, max_tokens=9000)
    b["resolved"]["day"].update(context=32768, runtime_revision="new")
    assert performance.cohort(a) == performance.cohort(b)
    for key, value in [
        ("task_manifest_hash", "other"),
        ("size", "quick"),
        ("difficulty", "large"),
        ("repetitions", 3),
        ("engine", "new"),
        ("review_mode", "human"),
        ("acceptance_version", 5),
    ]:
        changed = copy.deepcopy(b)
        changed["profile_spec"][key] = value
        assert performance.cohort(a) != performance.cohort(changed)
    b["mode"] = "parallel"
    assert performance.cohort(a) != performance.cohort(b)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "invalid"},
        {"status": "failed"},
        {"load_warning": "Unrelated request"},
    ],
)
def test_excluded_runs_are_unranked(change):
    runs = [run("1"), run("2", **change)]
    ss = {r["id"]: {"day": summary()} for r in runs}
    performance.rank_history(runs, ss)
    assert metric(ss["1"]["day"])["overall_rank"]["total"] == 1
    assert metric(ss["2"]["day"])["overall_rank"] is None


def test_partial_infrastructure_missing_and_method_are_separate():
    runs = [run(str(i)) for i in range(5)]
    ss = {r["id"]: {"day": summary()} for r in runs}
    ss["1"]["day"]["partial"] = True
    ss["2"]["day"]["infrastructure_error"] = "verifier unavailable"
    metric(ss["3"]["day"])["method"] = "different_measurement"
    metric(ss["4"]["day"])["value"] = None
    performance.rank_history(runs, ss)
    assert metric(ss["0"]["day"])["overall_rank"]["total"] == 1
    assert metric(ss["3"]["day"])["overall_rank"]["total"] == 1
    assert metric(ss["4"]["day"])["overall_rank"] is None


def test_successful_timing_requires_identical_tasks_and_review_feedback():
    runs = [run(str(i)) for i in range(3)]
    ss = {}
    for r in runs:
        s = summary()
        s["tasks"][0]["active_seconds"] = 10
        s["session_metrics"] = [
            {"name": "active_seconds", "label": "Active time", "value": 10}
        ]
        if r["id"] == "1":
            s["tasks"][0]["id"] = "other"
        if r["id"] == "2":
            s["tasks"][0]["review_history"] = [
                {"decision": "revise", "feedback": "new"}
            ]
        performance.attach(s)
        ss[r["id"]] = {"day": s}
    performance.rank_history(runs, ss)
    assert all(
        metric(s["day"], "active_seconds")["overall_rank"]["total"] == 1
        for s in ss.values()
    )
    assert metric(ss["0"]["day"])["overall_rank"]["total"] == 3


def test_history_scans_each_run_once_and_recomputes_after_deletion(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "DATA", tmp_path)
    db.initialize()
    for rid in ("1", "2"):
        r = run(rid)
        r["family"] = "quality"
        r["profile_spec"]["execution_adapter_version"] = 2
        with db.transaction() as c:
            db.put_run(c, r)
        atomic_json(tmp_path / "runs" / rid / "day" / "result.json", summary())
    original = results.summarize
    calls = []

    def spy(r):
        calls.append(r["id"])
        return original(r)

    monkeypatch.setattr(results, "summarize", spy)
    history = results.history()
    assert sorted(calls) == ["1", "2"]
    assert metric(history[0]["summary"]["day"])["model_rank"]["total"] == 2
    with db.transaction() as c:
        c.execute("DELETE FROM runs WHERE id='1'")
    calls.clear()
    result = results.enrich(db.get_run("2"))
    assert calls == ["2"]
    assert metric(result["summary"]["day"])["previous"] is None


def test_speed_legacy_score_is_unchanged_and_client_speed_uses_request_end(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "DATA", tmp_path)
    r = run("1", family="speed")
    raw = {
        "ok": True,
        "finish_reason": "stop",
        "decode_tps": 100,
        "ttft_ms": 1000,
        "wall_ms": 3000,
        "completion_tokens": 101,
    }
    path = tmp_path / "runs" / "1" / "day" / "decode.json"
    atomic_json(
        path, {"single_stream": {"code": [raw]}, "config": {"weights": {"code": 1}}}
    )
    before = path.read_bytes()
    s = results.summarize(r)["day"]
    assert s["score"] == 100
    assert metric(s)["value"] == 50
    assert metric(s, "benchmark_score")["value"] == 100
    assert metric(s, "benchmark_score")["label"] == "Weighted throughput"
    assert path.read_bytes() == before


def test_display_precision_ties_and_multiple_targets_do_not_compare_to_self():
    runs = [run("1"), run("2")]
    ss = {"1": {"day": summary()}, "2": {"day": summary(), "night": summary()}}
    metric(ss["1"]["day"])["value"] = 50.01
    metric(ss["2"]["day"])["value"] = 50.04
    performance.rank_history(runs, ss)
    for s in ss["2"].values():
        m = metric(s)
        assert m["model_rank"] == {"rank": 1, "total": 3, "tied": True}
        assert m["previous"]["run_id"] == "1"


def test_score_percentage_points_and_zero_previous_denominator():
    runs = [run("1"), run("2")]
    a, b = summary(), summary()
    metric(a, "task_score")["value"] = 0
    metric(b, "task_score")["value"] = 50
    metric(a, "ttft_seconds")["value"] = 0
    performance.rank_history(runs, {"1": {"day": a}, "2": {"day": b}})
    assert metric(b, "task_score")["previous"]["improvement"] == 50
    assert metric(b, "task_score")["previous"]["unit"] == "pp"
    assert metric(b, "ttft_seconds")["previous"]["improvement"] is None


def test_historical_prefill_is_an_estimate_and_missing_native_timing_is_explained():
    s = summary([request()])
    assert metric(s, "prompt_tps")["unavailable"] == "Not recorded"
    s = summary([request(timing_version=1)])
    assert metric(s, "prompt_tps")["unavailable"] == "Not exposed"


def test_harbor_persists_request_measurements_and_corruption_does_not_change_verdict(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from studio.harbor_agent import RouterLLM
    from studio.repository import collect_trials

    trial = tmp_path / "harbor" / "job" / "trial"

    async def complete(*args, **kwargs):
        return dict(
            request(), content="answer", reasoning_content="thinking", timing_version=1
        )

    monkeypatch.setattr("studio.harbor_agent.completion", complete)
    llm = RouterLLM("model", "http://router", {}, {}, trial / "agent")
    asyncio.run(llm.call("hello"))
    files = list((trial / "agent" / "performance-requests").glob("*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text())
    assert saved["usage"]["completion_tokens"] == 101
    assert "content" not in saved and "reasoning_content" not in saved
    atomic_json(
        trial / "result.json",
        {
            "task_name": "bench-studio/task",
            "verifier_result": {"rewards": {"reward": 1}},
        },
    )
    s = collect_trials(tmp_path, [{"id": "task"}])
    performance.attach(s)
    assert s["score"] == 100 and metric(s)["value"] == 50
    files[0].write_text("broken")
    s = collect_trials(tmp_path, [{"id": "task"}])
    assert s["score"] == 100
    assert s["tasks"][0]["requests"][0]["error"].startswith("Unreadable")


def test_quality_stream_retains_measurement_fields(monkeypatch):
    from studio import quality_worker

    chunks = [
        {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
        {
            "choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "timings": {"prompt_n": 30, "prompt_ms": 10, "cache_n": 70},
        },
    ]
    text = (
        "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
    )
    real_client = httpx.Client
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text=text))
    monkeypatch.setattr(
        quality_worker.httpx,
        "Client",
        lambda **kw: real_client(transport=transport, **kw),
    )
    value = quality_worker.generate(
        "http://router/v1",
        "model",
        {"id": "1", "language": "python", "prompt": "hello"},
        {
            "temperature": 0,
            "top_p": 1,
            "seed": 1,
            "max_tokens": 30,
            "reasoning_effort": "default",
        },
        lambda _: None,
    )
    assert value["timing_version"] == 1
    assert (
        value["ttft_ms"]
        <= value["ttfa_ms"]
        <= value["last_output_ms"]
        <= value["completion_ms"]
    )
    assert value["timings"]["cache_n"] == 70
    assert len(value["update_gaps_ms"]) == 1


def test_prefill_ranks_separate_skipped_depths():
    runs = [run("1", family="speed"), run("2", family="speed")]
    a, b = summary(), summary()
    a["metric"] = b["metric"] = "prefill_tps"
    a["tasks"][0]["id"] = "depth 2000 / 1"
    b["tasks"][0]["id"] = "depth 8000 / 1"
    performance.rank_history(runs, {"1": {"day": a}, "2": {"day": b}})
    assert metric(a)["overall_rank"]["total"] == 1
    assert metric(b)["overall_rank"]["total"] == 1
