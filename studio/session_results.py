"""Separate completion and time measurements, including failed and partial evidence."""

import hashlib
import json
import statistics


def median(values):
    return statistics.median(values) if values else None


def summarize_attempts(rows, profile, expected):
    valid = [r for r in rows if r["status"] in ("passed", "failed")]
    passed = [r for r in valid if r["status"] == "passed"]
    partial = len(valid) != expected
    vision = profile["family"] == "vision"
    rate = None if partial else 100 * len(passed) / expected
    metrics = [
        {
            "name": "accuracy" if vision else "commit_ready_rate",
            "label": "Accuracy"
            if vision
            else "Automated acceptance rate"
            if profile["suite"] == "visual-design"
            else "Commit-ready rate",
            "value": rate,
            "unit": "%",
            "direction": "higher",
        }
    ]
    if not vision:
        metrics.extend(
            [
                {
                    "name": "implementation_seconds",
                    "label": "Median implementation time · successful attempts",
                    "value": median([r["implementation_seconds"] for r in passed]),
                    "unit": "s",
                    "direction": "lower",
                },
                {
                    "name": "active_seconds",
                    "label": "Median active time · successful attempts",
                    "value": median([r["active_seconds"] for r in passed]),
                    "unit": "s",
                    "direction": "lower",
                },
            ]
        )
    metrics.append(
        {
            "name": "consumed_seconds",
            "label": "Active time · all attempts",
            "value": sum(r.get("active_seconds", 0) for r in rows),
            "unit": "s",
            "direction": "lower",
        }
    )
    requests = [q for r in rows for q in r.get("requests", [])]
    usage_complete = bool(requests) and all(q.get("usage") for q in requests)
    metrics.extend(
        [
            {
                "name": "request_seconds",
                "label": "Median request latency",
                "value": median(
                    [
                        q["elapsed_seconds"]
                        for q in requests
                        if q.get("elapsed_seconds") is not None
                    ]
                ),
                "unit": "s",
                "direction": "lower",
            },
            {
                "name": "ttft_ms",
                "label": "Median time to first token",
                "value": median(
                    [q["ttft_ms"] for q in requests if q.get("ttft_ms") is not None]
                ),
                "unit": "ms",
                "direction": "lower",
            },
        ]
    )
    return {
        "score": rate,
        "unit": "%",
        "metric": "vision_accuracy" if vision else "commit_ready_rate",
        "direction": "higher",
        "score_label": metrics[0]["label"],
        "passed": len(passed),
        "count": expected,
        "completed_count": len(valid),
        "partial": partial,
        "tasks": rows,
        "session_metrics": metrics,
        "metrics": [
            {
                "category": task,
                "rate": 100 * sum(r["status"] == "passed" for r in rr) / len(rr),
            }
            for task in sorted({r["task_id"] for r in valid})
            if (rr := [r for r in valid if r["task_id"] == task])
        ],
        "usage": {
            "prompt_tokens": sum(
                q.get("usage", {}).get("prompt_tokens", 0) for q in requests
            )
            if usage_complete
            else None,
            "completion_tokens": sum(
                q.get("usage", {}).get("completion_tokens", 0) for q in requests
            )
            if usage_complete
            else None,
            "complete": usage_complete,
            "ttft_ms_median": median(
                [q["ttft_ms"] for q in requests if q.get("ttft_ms") is not None]
            ),
            "requests": len(requests),
        },
        "compaction_count": sum(len(r.get("compactions", [])) for r in rows),
        "infrastructure_error": next(
            (r.get("detail") for r in rows if r["status"] == "infrastructure_error"),
            None,
        ),
    }


def feedback_signature(summary):
    value = {
        r["id"]: [
            x["feedback"]
            for x in r.get("review_history", [])
            if x.get("decision") == "revise"
        ]
        for r in summary.get("tasks", [])
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def paired_comparison(a, b):
    left = {r["id"]: r for r in a.get("tasks", []) if r["status"] == "passed"}
    right = {r["id"]: r for r in b.get("tasks", []) if r["status"] == "passed"}
    ids = sorted(left.keys() & right.keys())
    feedback_match = feedback_signature(a) == feedback_signature(b)
    rows = []
    for name in ("implementation_seconds", "active_seconds"):
        matched = [
            i
            for i in ids
            if left[i].get(name) is not None and right[i].get(name) is not None
        ]
        av = median([left[i][name] for i in matched])
        bv = median([right[i][name] for i in matched])
        rows.append(
            {
                "name": name,
                "a": av,
                "b": bv,
                "unit": "s",
                "direction": "lower",
                "matched_count": len(matched),
                "improvement_percent": (1 - bv / av) * 100
                if feedback_match and av and bv is not None
                else None,
            }
        )
    return {
        "matched_count": len(ids),
        "matched_attempts": ids,
        "feedback_matches": feedback_match,
        "timings": rows,
        "warning": None
        if feedback_match
        else "Revision feedback differs. Timing is descriptive; automatic speed-improvement claims are disabled.",
    }
