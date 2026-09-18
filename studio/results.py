import json
import statistics
from pathlib import Path
from betterbench.report import combined_score, single_rows, prefill_rows
from common import read_json
from . import config, db
from .repository import collect_trials


def baseline_slot(m, target):
    if m.get("family") in {"session", "vision"}:
        from .profiles import fingerprint

        return f"sessions-v1:{fingerprint(workload(m))}:{target}"
    return f"{m.get('profile')}:{m.get('profile_spec', {}).get('size', 'standard')}:{m.get('mode', 'sequential')}:{target}"


def import_legacy():
    for path in (config.DATA / "runs").glob("*/manifest.json"):
        try:
            m = read_json(path)
            if not m.get("id") or db.get_run(m["id"]):
                continue
            m["legacy"] = True
            if m["status"] in db.ACTIVE:
                m.update(
                    status="interrupted",
                    error="Imported without a live Studio worker; raw artifacts retained",
                )
            m["family"] = "speed"
            with db.transaction() as c:
                db.put_run(c, m)
        except (ValueError, OSError, KeyError):
            continue


def summarize(m):
    result = {}
    root = config.DATA / "runs" / m["id"]
    for target in m.get("requested_targets", []):
        item = {
            "target": target,
            "canonical": m.get("resolved", {}).get(target, {}).get("canonical", target),
            "score": None,
            "unit": "",
            "metrics": [],
            "tasks": [],
        }
        try:
            attempts = (
                sorted((root / target).glob("*/attempt.json"))
                if m.get("family") in {"session", "vision"}
                else []
            )
            if attempts:
                from .session_results import summarize_attempts

                p = m["profile_spec"]
                item.update(
                    summarize_attempts(
                        [read_json(path) for path in attempts],
                        p,
                        len(p["task_ids"]) * p["repetitions"],
                    )
                )
            elif (root / target / "result.json").exists():
                item.update(read_json(root / target / "result.json"))
            elif m.get("family") == "agent":
                tasks = m.get("repository_tasks")
                if not tasks and (root / "manifest.json").exists():
                    tasks = read_json(root / "manifest.json").get("repository_tasks")
                if tasks:
                    item.update(
                        collect_trials(root / target, tasks, job_error=m.get("error"))
                    )
            elif m.get("family") in {"session", "vision"}:
                from .session_results import summarize_attempts

                p = m["profile_spec"]
                rows = [
                    read_json(path)
                    for path in sorted((root / target).glob("*/attempt.json"))
                ]
                item.update(
                    summarize_attempts(rows, p, len(p["task_ids"]) * p["repetitions"])
                )
            elif (root / target / "decode.json").exists():
                data = read_json(root / target / "decode.json")
                rows = single_rows(data)
                score = combined_score(data, rows)
                item.update(
                    score=score.get("decode") if score else None,
                    unit="tok/s",
                    metric="decode_tps",
                    count=sum(r["runs"] for r in rows),
                    metrics=rows,
                )
                item["tasks"] = [
                    {
                        "id": f"{cat}/{i + 1}",
                        "status": "passed" if r.get("ok") else "failed",
                        "duration": r.get("elapsed_s"),
                        "ttft_ms": r.get("ttft_ms"),
                        "decode_tps": r.get("decode_tps"),
                        "finish_reason": r.get("finish_reason"),
                        "completion_tokens": r.get("completion_tokens"),
                    }
                    for cat, rr in data.get("single_stream", {}).items()
                    for i, r in enumerate(rr)
                ]
            elif (root / target / "prefill.json").exists():
                data = read_json(root / target / "prefill.json")
                rows = prefill_rows(data)
                values = [r["pp_med"] for r in rows if not r.get("skipped")]
                item.update(
                    score=statistics.median(values) if values else None,
                    unit="prompt tok/s",
                    metric="prefill_tps",
                    metrics=rows,
                    count=sum(r.get("pp_n", 0) for r in rows),
                    score_label="Median of depth medians",
                )
                for depth in data.get("prefill", []):
                    if depth.get("skipped"):
                        item["tasks"].append(
                            {
                                "id": f"depth {depth['target_depth']}",
                                "status": "skipped",
                                "detail": depth.get("reason", "Depth unavailable"),
                            }
                        )
                        continue
                    for i, rate in enumerate(depth.get("pp_tps", [])):
                        item["tasks"].append(
                            {
                                "id": f"depth {depth['target_depth']} / {i + 1}",
                                "status": "passed",
                                "prefill_tps": rate,
                                "prompt_tokens": depth.get("prompt_tokens", [])[i],
                                "ttft_ms": depth.get("ttft_ms", [])[i],
                            }
                        )
        except (ValueError, KeyError, IndexError, TypeError, OSError) as e:
            item["summary_error"] = str(e)
            item["score"] = None
        if (
            m["status"] != "completed"
            or item.get("infrastructure_error")
            or item.get("partial")
        ):
            item["score"] = None
        if (
            m.get("family") in ["quality", "agent"]
            and m.get("profile_spec", {}).get("execution_adapter_version", 1) < 2
        ):
            item["validation_warning"] = (
                "Historical v1 result: predates verifier and task-validation fixes. Measurements are preserved; use a v2 profile for a corrected comparison."
            )
        result[target] = item
    return result


def enrich(m):
    m = dict(m)
    m["summary"] = summarize(m)
    root = config.DATA / "runs" / m["id"]
    m["artifacts"] = (
        [
            str(p.relative_to(root))
            for p in root.rglob("*")
            if p.is_file()
            and not p.is_symlink()
            and p.stat().st_size < 100 * 1024 * 1024
        ][:2000]
        if root.exists()
        else []
    )
    for t, s in m["summary"].items():
        slot = baseline_slot(m, t)
        with db.connect() as c:
            r = c.execute("SELECT * FROM baselines WHERE slot=?", (slot,)).fetchone()
        if r:
            base = db.get_run(r["run_id"])
            if base and base["status"] == "completed":
                bs = summarize(base).get(r["target"], {})
                s["baseline"] = {
                    "run_id": r["run_id"],
                    "target": r["target"],
                    "score": bs.get("score"),
                }
                if (
                    s.get("score") is not None
                    and bs.get("score") is not None
                    and s.get("metric") == bs.get("metric")
                    and comparable(m, base)
                ):
                    s["delta"] = (
                        s["score"] - bs["score"]
                        if s["unit"] == "%"
                        else (
                            (s["score"] / bs["score"] - 1) * 100
                            if bs["score"]
                            else None
                        )
                    )
                    s["delta_unit"] = "pp" if s["unit"] == "%" else "%"
    if m.get("family") in {"session", "vision"}:
        from .session_reviews import reviews

        m["reviews"] = reviews(m["id"])
    return m


def workload(m):
    p = m.get("profile_spec", {})
    base = (
        m.get("family", "speed"),
        m.get("profile"),
        p.get("size", "standard"),
        p.get("engine"),
        p.get("version"),
        m.get("mode", "sequential"),
        p.get("task_manifest_hash"),
        p.get("execution_adapter_version", 1),
    )
    if m.get("family") in {"session", "vision"}:
        return base + (
            p.get("difficulty"),
            p.get("task_selection"),
            p.get("repetitions"),
            p.get("review_mode"),
            p.get("acceptance_version"),
            p.get("compaction_version"),
            p.get("session_image"),
            p.get("parameters", {}).get("task_timeout"),
            p.get("parameters", {}).get("max_turns"),
        )
    return base


def comparable(a, b):
    return workload(a) == workload(b)


def compare(a, b, ta, tb):
    if a["status"] != "completed" or b["status"] != "completed":
        raise ValueError("Only completed runs can be compared")
    sa, sb = summarize(a).get(ta), summarize(b).get(tb)
    if (
        not sa
        or not sb
        or sa.get("score") is None
        or sb.get("score") is None
        or sa.get("metric") != sb.get("metric")
        or not comparable(a, b)
    ):
        raise ValueError(
            "Select the same profile, size, engine, and execution mode; metrics must match"
        )
    aa = a.get("resolved", {}).get(ta, {})
    bb = b.get("resolved", {}).get(tb, {})
    fields = {
        "model": (aa.get("canonical"), bb.get("canonical")),
        "context": (aa.get("context"), bb.get("context")),
        "runtime revision": (aa.get("runtime_revision"), bb.get("runtime_revision")),
        "GPU pair": (
            aa.get("service", {}).get("gpu_names"),
            bb.get("service", {}).get("gpu_names"),
        ),
        "engine image": (
            aa.get("service", {}).get("image_id"),
            bb.get("service", {}).get("image_id"),
        ),
    }
    fields["application revision"] = (a.get("revision"), b.get("revision"))
    if a.get("family") in {"session", "vision"}:
        fields["vision configuration"] = (
            a.get("host", {}).get("vision", {}).get(ta),
            b.get("host", {}).get("vision", {}).get(tb),
        )
    for role in ["worker_image", "verifier_image", "runner_image"]:
        fields[role] = (a.get("host", {}).get(role), b.get("host", {}).get(role))
    for k in set(a.get("profile_spec", {}).get("parameters", {})) | set(
        b.get("profile_spec", {}).get("parameters", {})
    ):
        fields[k] = (
            a.get("profile_spec", {}).get("parameters", {}).get(k),
            b.get("profile_spec", {}).get("parameters", {}).get(k),
        )
    result = {
        "a": sa,
        "b": sb,
        "differences": [
            {"field": k, "a": v[0], "b": v[1]}
            for k, v in fields.items()
            if v[0] != v[1]
        ],
        "warning": "Independent runs: descriptive differences, not evidence of causation.",
        "a_run": a["id"],
        "b_run": b["id"],
    }
    if a.get("family") in {"session", "vision"}:
        from .session_results import paired_comparison

        result["paired"] = paired_comparison(sa, sb)
    return result
