import hashlib
import json
import os
import statistics
import threading
import zlib
from betterbench.report import combined_score, single_rows, prefill_rows
from common import model_fingerprint, read_json
from . import config, db
from .repository import collect_trials
from . import performance

# Raw per-update and per-token latency series. performance.attach() reduces them
# to metrics; the series remain in run artifacts and exports, not in API JSON.
RAW_SERIES = ("update_gaps_ms", "itl_ms")
# Run-document fields that only the run detail view uses.
DETAIL_FIELDS = ("host", "generation", "session_progress", "repository_tasks")
# Run ID -> summaries derived from one run document and artifact state.
_summary_cache = {}
_summary_lock = threading.Lock()


def baseline_slot(m, target):
    # Results belong to the model measured: runs target service IDs but are
    # keyed by the canonical model pinned for the run (older runs targeted
    # canonical IDs, so their slots are unchanged).
    model = m.get("resolved", {}).get(target, {}).get("canonical") or target
    if m.get("family") in {"session", "vision"}:
        from .profiles import fingerprint

        return f"sessions-v1:{fingerprint(workload(m))}:{model}"
    return f"{m.get('profile')}:{m.get('profile_spec', {}).get('size', 'standard')}:{m.get('mode', 'sequential')}:{model}"


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
                # Deleted legacy manifests must not reappear after a restart,
                # including when filesystem cleanup could not finish.
                if (
                    c.execute(
                        "SELECT 1 FROM deleted_runs WHERE id=?", (m["id"],)
                    ).fetchone()
                    or c.execute("SELECT 1 FROM runs WHERE id=?", (m["id"],)).fetchone()
                ):
                    continue
                db.put_run(c, m)
        except (ValueError, OSError, KeyError):
            continue


def summarize(m):
    result = {}
    root = config.DATA / "runs" / m["id"]
    for target in m.get("requested_targets", []):
        resolved = m.get("resolved", {}).get(target, {})
        router = resolved.get("router") or {}
        item = {
            "target": target,
            "canonical": resolved.get("canonical", target),
            # Report facts from the router at launch; not part of the identity.
            "service": router.get("service"),
            "configuration": router.get("configuration"),
            "capability_score": router.get("capability_score"),
            "nsfw": router.get("nsfw"),
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
            elif m.get("family") == "quality" and (root / target / "responses.json").exists():
                item["tasks"] = [
                    dict({k: v for k, v in q.items() if k not in ("response", "reasoning")},
                         status="failed" if q.get("error") else "not_graded")
                    for q in read_json(root / target / "responses.json")
                ]
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
                        **r,
                        "id": f"{cat}/{i + 1}",
                        "status": "passed" if r.get("ok") else "failed",
                        "duration": r.get("wall_ms", 0) / 1000 if r.get("wall_ms") is not None else r.get("elapsed_s"),
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
                    if "requests" in depth:
                        item["tasks"].extend(
                            dict(r, id=f"depth {depth['target_depth']} / {i + 1}",
                                 status="passed" if r.get("ok") else "failed",
                                 prefill_tps=r.get("pp_tps"))
                            for i, r in enumerate(depth["requests"])
                        )
                        continue
                    for i, rate in enumerate(depth.get("pp_tps", [])):
                        item["tasks"].append(
                            {
                                "id": f"depth {depth['target_depth']} / {i + 1}",
                                "status": "passed",
                                "finish_reason": "stop",
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
        performance.attach(item)
        result[target] = item
    return result


def artifact_state(rid):
    """Metadata for every file in a run directory.

    Artifacts are written by atomic rename, so adding, removing, or rewriting
    any summary input changes this value.
    """
    files = []
    pending = [str(config.DATA / "runs" / rid)]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                            continue
                        st = entry.stat()
                        files.append(
                            (entry.path, st.st_ino, st.st_mtime_ns, st.st_size)
                        )
                    except OSError:
                        files.append((entry.path, None, None, None))
        except OSError:
            continue
    return sorted(files)


def summary_signature(m):
    value = [str(config.DATA), m, artifact_state(m["id"])]
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def strip_raw_series(value):
    """Remove raw latency series anywhere in task evidence (requests, compactions)."""
    if isinstance(value, dict):
        for key in RAW_SERIES:
            value.pop(key, None)
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return
    for child in children:
        strip_raw_series(child)


def summary_entry(m):
    """Summaries are recomputed only when the run or its artifacts change."""
    signature = summary_signature(m)
    entry = _summary_cache.get(m["id"])
    if entry is not None and entry["signature"] == signature:
        return entry
    with _summary_lock:
        entry = _summary_cache.get(m["id"])
        if entry is not None and entry["signature"] == signature:
            return entry
        summaries = summarize(m)
        facts, tasks = {}, {}
        for target, item in summaries.items():
            strip_raw_series(item.get("tasks"))
            facts[target] = performance.ranking_facts(item)
            tasks[target] = item.pop("tasks", [])
        # Store JSON so every request gets independent copies to rank and
        # annotate. Task rows are compressed and decoded only for detail views.
        entry = {
            "signature": signature,
            "summaries": json.dumps(summaries, default=str),
            "tasks": zlib.compress(json.dumps(tasks, default=str).encode(), 1),
            "facts": facts,
        }
        _summary_cache[m["id"]] = entry
        return entry


def cached_summaries(m, *, tasks=False):
    """Return (summaries, ranking facts); omit task rows unless requested."""
    entry = summary_entry(m)
    summaries = json.loads(entry["summaries"])
    if tasks:
        for target, rows in json.loads(zlib.decompress(entry["tasks"])).items():
            summaries[target]["tasks"] = rows
    return summaries, entry["facts"]


def history_snapshot(extra=(), tasks=()):
    runs = {m["id"]: m for m in db.runs()}
    runs.update({m["id"]: m for m in extra})
    summaries, facts = {}, {}
    for rid, m in runs.items():
        summaries[rid], facts[rid] = cached_summaries(m, tasks=rid in tasks)
    with _summary_lock:
        for rid in set(_summary_cache) - set(runs):
            del _summary_cache[rid]
    performance.rank_history(list(runs.values()), summaries, facts)
    with db.connect() as c:
        baselines = {r["slot"]: dict(r) for r in c.execute("SELECT * FROM baselines")}
    return runs, summaries, baselines


def history():
    """History rows: scores and rankings without per-task or artifact detail."""
    snapshot = history_snapshot()
    return [enrich(m, snapshot, detail=False) for m in snapshot[0].values()]


def enrich(m, snapshot=None, *, detail=True):
    runs, summaries, baselines = (
        snapshot
        if snapshot is not None
        else history_snapshot([m], tasks={m["id"]} if detail else ())
    )
    m = dict(m)
    m["model_fingerprints"] = {}
    for target, resolved in m.get("resolved", {}).items():
        try:
            m["model_fingerprints"][target] = model_fingerprint(resolved)
        except (KeyError, TypeError, ValueError):
            # Old imported manifests may not contain a complete identity.
            # Their reruns require the operator to select a model explicitly.
            pass
    m["summary"] = summaries[m["id"]]
    if detail:
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
        r = baselines.get(slot)
        if r:
            base = runs.get(r["run_id"])
            if base and base["status"] == "completed":
                bs = summaries[base["id"]].get(r["target"], {})
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
    if not detail:
        for key in DETAIL_FIELDS:
            m.pop(key, None)
        return m
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
    _, summaries, _ = history_snapshot([a, b], tasks={a["id"], b["id"]})
    sa, sb = summaries[a["id"]].get(ta), summaries[b["id"]].get(tb)
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
        "GPUs": (
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
