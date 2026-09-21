"""Read-only performance projections and workload-scoped historical rankings.

Native benchmark scores and stored artifacts are never changed here. Request
speed uses real usage and the same client measurement points across families.
"""

import hashlib
import json
import math
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP


# id, label, unit, better direction, displayed decimals, measurement method
METRICS = [
    ("output_tps", "Output speed", "tok/s", "higher", 1, "client_output_v1"),
    ("ttft_seconds", "First token", "s", "lower", 2, "client_first_output_v1"),
    ("prompt_tps", "Prompt processing", "tok/s", "higher", 1, "backend_prompt_v1"),
    ("request_seconds", "Request latency", "s", "lower", 2, "client_request_v1"),
    ("tpot_ms", "Time per output token", "ms", "lower", 2, "client_tpot_v1"),
    ("ttfa_seconds", "First answer", "s", "lower", 2, "client_first_answer_v1"),
    ("update_gap_ms", "Inter-update latency", "ms", "lower", 2, "client_update_gap_v1"),
    (
        "prompt_estimate_tps",
        "Prompt throughput estimate",
        "tok/s",
        "higher",
        1,
        "prompt_tokens_over_ttft_v1",
    ),
    (
        "native_output_tps",
        "Backend generation speed",
        "tok/s",
        "higher",
        1,
        "backend_output_v1",
    ),
]


def number(value, *, positive=False):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and (value > 0 if positive else value >= 0)
    )


def request_values(q):
    """Missing and failed evidence stays missing; task failure is independent."""
    if (
        q.get("error")
        or q.get("ok") is False
        or q.get("finish_reason") not in ("stop", "length")
    ):
        return {}
    values = {}
    elapsed = q.get("elapsed_seconds", q.get("duration"))
    if elapsed is None and number(q.get("wall_ms")):
        elapsed = q["wall_ms"] / 1000
    ttft = q.get("ttft_ms")
    usage = q.get("usage") or q
    if not isinstance(usage, dict):
        usage = {}
    tokens = usage.get("completion_tokens")
    if number(elapsed, positive=True):
        values["request_seconds"] = elapsed
    if number(ttft) and (elapsed is None or number(elapsed) and ttft / 1000 <= elapsed):
        values["ttft_seconds"] = ttft / 1000
        if (
            type(tokens) is int
            and tokens > 1
            and number(elapsed, positive=True)
            and elapsed > ttft / 1000
        ):
            decode_seconds = elapsed - ttft / 1000
            values["output_tps"] = (tokens - 1) / decode_seconds
            values["tpot_ms"] = 1000 * decode_seconds / (tokens - 1)
        prompt = usage.get("prompt_tokens")
        if type(prompt) is int and prompt > 0 and ttft > 0:
            values["prompt_estimate_tps"] = prompt / (ttft / 1000)
    if number(q.get("ttfa_ms")):
        values["ttfa_seconds"] = q["ttfa_ms"] / 1000
    timings = q.get("timings") or {}
    if not isinstance(timings, dict):
        timings = {}
    for name, count, duration in (
        ("prompt_tps", "prompt_n", "prompt_ms"),
        ("native_output_tps", "predicted_n", "predicted_ms"),
    ):
        if (
            type(timings.get(count)) is int
            and timings[count] > 0
            and number(timings.get(duration), positive=True)
        ):
            values[name] = 1000 * timings[count] / timings[duration]
    raw_gaps = q.get("update_gaps_ms")
    gaps = [v for v in raw_gaps if number(v)] if isinstance(raw_gaps, list) else []
    if gaps:
        values["update_gap_ms"] = gaps
    return {k: v for k, v in values.items() if isinstance(v, list) or number(v)}


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def metric(spec, samples, total):
    name, label, unit, direction, precision, method = spec
    samples = [v for v in samples if number(v)]
    value = statistics.median(samples) if samples else None
    return {
        "id": name,
        "label": label,
        "value": value,
        "unit": unit,
        "direction": direction,
        "precision": precision,
        "method": method,
        "samples": len(samples),
        "total": total,
        "unavailable": None
        if samples
        else "Not exposed"
        if name in {"prompt_tps", "native_output_tps"}
        else "Not recorded",
        "distribution": {
            "q1": percentile(samples, 0.25),
            "q3": percentile(samples, 0.75),
            "tail": percentile(samples, 0.05 if direction == "higher" else 0.95)
            if len(samples) >= 100
            else None,
            "tail_label": "p5" if direction == "higher" else "p95",
        }
        if samples
        else None,
    }


def request_summary(requests):
    measured = [request_values(q) for q in requests]
    metrics = []
    for spec in METRICS:
        name = spec[0]
        samples = []
        for values in measured:
            value = values.get(name)
            if isinstance(value, list):
                samples.extend(value)
            elif value is not None:
                samples.append(value)
        m = metric(spec, samples, len(requests))
        if not samples and name in {"prompt_tps", "native_output_tps"}:
            recorded = any(q.get("timing_version") for q in requests)
            m["unavailable"] = "Not exposed" if recorded else "Not recorded"
            m["unavailable_reason"] = (
                "The backend did not expose processing timings."
                if recorded
                else "Backend processing timings were not saved for this run."
            )
        if name == "update_gap_ms":
            m["coverage_unit"] = "stream gaps"
            m["reported_requests"] = sum(name in v for v in measured)
        metrics.append(m)
    return {
        "metrics": metrics,
        "requests": len(requests),
        "request_errors": sum(
            bool(q.get("error")) or q.get("ok") is False for q in requests
        ),
        "output_limit_requests": sum(
            q.get("finish_reason") == "length" for q in requests
        ),
    }


def attach(summary):
    """Attach per-request, per-task, and run projections to a fresh summary."""
    requests = []
    for task in summary.get("tasks", []):
        rows = task.get("requests")
        if rows is None:
            rows = (
                [task]
                if any(k in task for k in ("usage", "ttft_ms", "wall_ms", "duration"))
                else []
            )
        for q in rows:
            # Compact per-request values; never recursively copy task evidence.
            q["performance_values"] = request_values(q)
            q["performance_values"].pop("update_gap_ms", None)
        task["performance"] = request_summary(rows)
        requests.extend(rows)
    performance = request_summary(requests)
    score = summary.get("score")
    if summary.get("unit") == "%" or summary.get("metric") in {
        "decode_tps",
        "prefill_tps",
    }:
        percentage = summary.get("unit") == "%"
        score_metric = metric(
            (
                "task_score" if percentage else "benchmark_score",
                summary.get("score_label", "Task pass rate")
                if percentage
                else "Weighted throughput"
                if summary.get("metric") == "decode_tps"
                else "Prompt estimate · median of depth medians",
                summary.get("unit"),
                "higher",
                1,
                summary.get("metric", "task_score"),
            ),
            [score] if number(score) else [],
            summary.get("count", 0),
        )
        score_metric["samples"] = (
            summary.get("completed_count", summary.get("count", 0))
            if number(score)
            else 0
        )
        score_metric["distribution"] = None
        performance["metrics"].append(score_metric)
    for m in summary.get("session_metrics", []):
        if m["name"] in {"active_seconds", "implementation_seconds"}:
            samples = [
                t[m["name"]]
                for t in summary.get("tasks", [])
                if t.get("status") == "passed" and number(t.get(m["name"]))
            ]
            row = metric(
                (
                    m["name"],
                    m["label"],
                    "s",
                    "lower",
                    1,
                    "successful_attempt_median_v1",
                ),
                samples,
                summary.get("count", 0),
            )
            performance["metrics"].append(row)
    summary["performance"] = performance


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def cohort(run):
    p = run.get("profile_spec", {})
    # Hash actual workload definitions, not profile names, IDs or tuning knobs.
    keys = (
        "family",
        "suite",
        "size",
        "difficulty",
        "task_selection",
        "repetitions",
        "review_mode",
        "engine",
        "version",
        "execution_adapter_version",
        "acceptance_version",
        "compaction_version",
        "session_image",
        "fixture_source_hash",
        "base_revisions",
        "task_manifest_hash",
        "task_ids",
        "languages",
        "dataset_versions",
        "dataset_version",
        "pinned_tasks",
        "screenshot_renderer",
    )
    definition = {k: p[k] for k in keys if k in p}
    definition["family"] = run.get("family", "speed")
    definition["mode"] = run.get("mode", "sequential")
    definition["budgets"] = {
        k: p.get("parameters", {}).get(k) for k in ("task_timeout", "max_turns")
    }
    if p.get("spec"):
        spec = json.loads(json.dumps(p["spec"]))
        for k in (
            "temperature",
            "top_p",
            "top_k",
            "seed",
            "greedy",
            "reasoning_effort",
            "max_tokens",
        ):
            spec.get("config", {}).pop(k, None)
        definition["spec"] = spec
    elif not p.get("task_manifest_hash") and not p.get("suite"):
        # Old artifacts lack a content identity. Do not pool unrelated suites.
        definition["legacy_workload"] = p.get("parent", run.get("profile"))
    return digest(definition)


def conditions(run, target):
    resolved = run.get("resolved", {}).get(target, {})
    service = resolved.get("service", {})
    canonical = resolved.get("canonical", target)
    return {
        "model": canonical,
        "context": resolved.get("context"),
        "quantization": resolved.get("metadata", {}).get("quantization"),
        "model revision": resolved.get("metadata", {}).get("revision"),
        "runtime revision": resolved.get("runtime_revision"),
        "engine image": service.get("image_id"),
        "GPUs": service.get("gpu_names"),
        "engine arguments": run.get("host", {}).get("engine_args", {}).get(canonical),
        **run.get("profile_spec", {}).get("parameters", {}),
    }


def exclusion(run, summary):
    if run.get("status") != "completed":
        return "Only completed runs are ranked"
    if (
        summary.get("partial")
        or summary.get("infrastructure_error")
        or summary.get("summary_error")
    ):
        return "Incomplete or invalid benchmark evidence"
    if run.get("load_warning"):
        return "Unrelated runtime activity detected"
    return None


def rounded(m):
    return Decimal(str(m["value"])).quantize(
        Decimal(10) ** -m["precision"], rounding=ROUND_HALF_UP
    )


def rank_history(runs, summaries):
    """One in-memory snapshot; group/sort once, with no per-peer artifact I/O."""
    groups = defaultdict(list)
    for run in runs:
        for target, summary in summaries[run["id"]].items():
            perf = summary["performance"]
            perf["cohort"] = cohort(run)
            if summary.get("metric") == "prefill_tps":
                # A shorter context window can skip entire sweep depths. Those
                # partial sweeps must not outrank runs that measured all depths.
                depths = sorted(
                    {
                        t["id"].split(" / ")[0]
                        for t in summary.get("tasks", [])
                        if t.get("status") == "passed"
                    }
                )
                perf["cohort"] = digest([perf["cohort"], depths])
            perf["ranking_unavailable"] = exclusion(run, summary)
            perf["conditions"] = conditions(run, target)
            model = summary["canonical"]
            order = (
                run.get("finished_at") or run.get("created_at", ""),
                run["id"],
                target,
            )
            for m in perf["metrics"]:
                m.update(model_rank=None, overall_rank=None, previous=None)
                if perf["ranking_unavailable"] or m["value"] is None:
                    continue
                success = None
                if m["id"] in {"active_seconds", "implementation_seconds"}:
                    from .session_results import feedback_signature

                    success = digest(
                        [
                            sorted(
                                t["id"]
                                for t in summary.get("tasks", [])
                                if t.get("status") == "passed"
                            ),
                            feedback_signature(summary),
                        ]
                    )
                key = (perf["cohort"], m["id"], m["method"], success)
                entry = (order, model, run, target, m, perf)
                groups[key].append(entry)
    for peers in groups.values():
        all_values = sorted(rounded(e[4]) for e in peers)
        models = defaultdict(list)
        for e in peers:
            models[e[1]].append(e)
        for same_model in models.values():
            model_values = sorted(rounded(e[4]) for e in same_model)
            previous = None
            prior_run = None
            for entry in sorted(same_model, key=lambda e: e[0]):
                order, model, run, target, m, perf = entry
                value = rounded(m)
                for name, values in (
                    ("model_rank", model_values),
                    ("overall_rank", all_values),
                ):
                    better = (
                        bisect_left(values, value)
                        if m["direction"] == "lower"
                        else len(values) - bisect_right(values, value)
                    )
                    m[name] = {
                        "rank": better + 1,
                        "total": len(values),
                        "tied": bisect_right(values, value) - bisect_left(values, value)
                        > 1,
                    }
                # Two targets in one run are separate entries, never predecessors.
                if previous and previous[2]["id"] != run["id"]:
                    prior_run = previous
                earlier = prior_run
                if earlier:
                    old = earlier[4]["value"]
                    delta = m["value"] - old
                    improvement = delta if m["direction"] == "higher" else -delta
                    change = (
                        improvement
                        if m["unit"] == "%"
                        else 100 * improvement / old
                        if old
                        else None
                    )
                    before = earlier[5]["conditions"]
                    m["previous"] = {
                        "run_id": earlier[2]["id"],
                        "target": earlier[3],
                        "value": old,
                        "improvement": change,
                        "absolute_change": delta,
                        "unit": "pp" if m["unit"] == "%" else "%",
                        "differences": [
                            {"field": k, "a": before.get(k), "b": v}
                            for k, v in perf["conditions"].items()
                            if before.get(k) != v
                        ],
                    }
                previous = entry
