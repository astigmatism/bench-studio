"""Discovery, validation and static report indexing; no generation or Docker calls."""

from __future__ import annotations

import fcntl
import html
import hashlib
import re
import json
import math
import os
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ACTIVE = {"starting", "running", "stopping"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    return json.loads(Path(path).read_text())


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, indent=2, allow_nan=False) + "\n")


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def snapshot(settings):
    return {
        "observed_at": now(),
        "runtime": get_json(settings["runtime_url"]),
        "models": get_json(settings["endpoint"] + "/models"),
    }


def safe_target(name):
    """Stable path/container identifier for provider names containing slashes or Unicode."""
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}", name) and name not in {
        ".",
        "..",
    }:
        return name
    return "model-" + hashlib.sha256(name.encode()).hexdigest()[:20]


class RuntimeUnavailable(RuntimeError):
    """A readiness observation, distinct from a changed model identity."""


def resolve(snap, target, *, require_healthy=True):
    row = next(
        (
            r
            for r in snap["models"]["data"]
            if r["id"] == target or safe_target(r["id"]) == target
        ),
        None,
    )
    if row is None:
        raise RuntimeError(f"Model alias {target!r} is absent from discovery")
    meta = row.get("x_ollama_router", {})
    if not meta.get("complete"):
        raise RuntimeError(f"Model {target} has incomplete metadata")
    if require_healthy and not meta.get("health", {}).get("available"):
        raise RuntimeUnavailable(f"Model {target} is temporarily unavailable")
    context = meta.get("context_window")
    if not isinstance(context, int) or context <= 0:
        raise RuntimeError(f"Model {target} has no valid context window")
    canonical = meta.get("upstream_model") or row["id"]
    service = next(
        (s for s in snap["runtime"].get("services", []) if s["model"] == canonical),
        None,
    )
    if not service:
        raise RuntimeError(f"Model/runtime configuration changed during the run: missing {target}: {canonical}")
    if require_healthy and (not service.get("healthy") or not service.get("running")):
        raise RuntimeUnavailable(f"Runtime has no healthy service for {target}: {canonical}")
    return {
        "alias": target,
        "canonical": canonical,
        "context": context,
        "reserve": meta.get("context_safety_reserve", 1024),
        "metadata": meta,
        "service": service,
        "runtime_revision": snap["runtime"].get("deployed_revision"),
        "runtime_profile": snap["runtime"].get("profile"),
    }


def identity(resolved):
    m, s = resolved["metadata"], resolved["service"]
    return {
        "canonical": resolved["canonical"],
        "context": resolved["context"],
        "reserve": resolved["reserve"],
        "model_revision": m.get("revision"),
        "quantization": m.get("quantization"),
        "reasoning": m.get("reasoning"),
        "container_id": s.get("id"),
        "image_id": s.get("image_id"),
        "runtime_revision": resolved["runtime_revision"],
        "runtime_profile": resolved["runtime_profile"],
    }


def require_idle(snap, targets, *, all_services=True):
    runtime = snap["runtime"]
    if not runtime.get("ready") or runtime.get("maintenance", {}).get("draining"):
        raise RuntimeError("AI Runtime is not ready or is draining")
    maintenance = runtime.get("maintenance", {})
    if all_services and (
        maintenance.get("active_requests", 0) or maintenance.get("queued_requests", 0)
    ):
        raise RuntimeError("Router has existing active/queued work; retry when idle")
    services = (
        runtime.get("services", [])
        if all_services
        else [resolve(snap, t)["service"] for t in targets]
    )
    if any(s.get("processing") for s in services):
        busy = ", ".join(s["name"] for s in services if s.get("processing"))
        raise RuntimeError(f"Backend processing is active ({busy}); retry when idle")
    for target in targets:
        resolve(snap, target)


def check_drift(before, after):
    service = after.get("service", {})
    if (identity(before) != identity(after) or service.get("differences")
        or any(before.get("service", {}).get(k) != service.get(k)
               for k in ("started_at", "restart_count") if k in before.get("service", {}))):
        raise RuntimeError(
            f"Model/runtime configuration changed during the run for {before['alias']}"
        )


def wait_for_runtime(settings, target, expected, *, timeout=90):
    """Wait before a new request; never retry an inference request."""
    import time
    from urllib.error import URLError
    deadline = time.monotonic() + timeout
    while True:
        try:
            snap = snapshot(settings)
            observed = resolve(snap, target, require_healthy=False)
            check_drift(expected, observed)
            return resolve(snap, target)
        except (RuntimeUnavailable, URLError, TimeoutError) as exc:
            if time.monotonic() >= deadline:
                raise RuntimeUnavailable(f"Runtime readiness did not recover within {timeout}s: {exc}") from exc
            time.sleep(min(3, max(0, deadline - time.monotonic())))


def valid_sample(row):
    if not row.get("ok") or row.get("error"):
        raise RuntimeError(f"Request failed: {row.get('error') or 'ok=false'}")
    if row.get("finish_reason") not in {"stop", "length"}:
        raise RuntimeError(
            f"Missing/invalid terminal finish reason: {row.get('finish_reason')}"
        )
    for key in ("prompt_tokens", "completion_tokens"):
        v = row.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise RuntimeError(
                f"Missing real token usage: {key}={v}; refusing chunk-count estimates"
            )
    for key in ("ttft_ms", "decode_tps"):
        v = row.get(key)
        if not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise RuntimeError(f"Missing/invalid measured {key}: {v}")


def validate_results(data, cfg, categories):
    if cfg["run_single_stream"]:
        if set(data.get("single_stream", {})) != set(categories):
            raise RuntimeError("Result categories do not match the requested profile")
        for rows in data["single_stream"].values():
            if len(rows) != cfg["runs_per_category"]:
                raise RuntimeError("Measured request count does not match profile")
            for row in rows:
                valid_sample(row)
    if cfg["run_prefill"]:
        rows = data.get("prefill", [])
        if len(rows) != len(cfg["prefill_depths"]):
            raise RuntimeError("Prefill result depth count does not match profile")
        measured = 0
        for row in rows:
            if row.get("skipped"):
                continue
            for key in ("prompt_tokens", "ttft_ms", "pp_tps"):
                vals = row.get(key, [])
                if len(vals) != cfg["prefill_runs"] or any(
                    not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
                    for v in vals
                ):
                    raise RuntimeError(
                        f"Prefill depth {row.get('target_depth')} has invalid {key}"
                    )
            measured += 1
        if not measured:
            raise RuntimeError("No prefill depths produced valid measurements")


def render_index(root):
    root = Path(root)
    rows = []
    manifests = sorted((root / "runs").glob("*/manifest.json"), reverse=True)
    esc = lambda s: html.escape(str(s), quote=True)
    for p in manifests:
        try:
            m = read_json(p)
        except (ValueError, OSError):
            continue
        rid = m["id"]
        links = []
        for target, info in m.get("targets", {}).items():
            for report in info.get("reports", []):
                rel = f"runs/{rid}/{target}/{report}"
                links.append(
                    f'<a href="{esc(rel)}">{esc(target)} {esc(Path(report).stem)}</a>'
                )
        links += [
            f'<a href="runs/{esc(rid)}/run.log">Log</a>',
            f'<a href="runs/{esc(rid)}/manifest.json">Metadata</a>',
            f'<a href="runs/{esc(rid)}/">Files / JSON</a>',
        ]
        status = m["status"]
        if status in ACTIVE:
            updated = datetime.fromisoformat(m.get("updated_at", m["created_at"]))
            if (datetime.now(timezone.utc) - updated).total_seconds() > 90:
                status = "unverified"
                m["error"] = (
                    "Worker heartbeat is stale. Run ./bench status to reconcile; the last saved state may be interrupted."
                )
        cls = (
            "good"
            if status == "completed"
            else ("bad" if status in {"failed", "interrupted", "invalid"} else "active")
        )
        details = m.get("error") or m.get("progress", "")
        rows.append(
            f'<tr><td><code>{esc(rid)}</code><small>{esc(m["created_at"])}</small></td>'
            f'<td>{esc(", ".join(m["requested_targets"]))}<small>{esc(m["profile"])} · {esc(m["mode"])}</small></td>'
            f'<td><strong class="{cls}">{esc(status)}</strong><small>{esc(details)}</small></td>'
            f'<td>{"<br>".join(links)}</td></tr>'
        )
    body = (
        "".join(rows)
        or '<tr><td colspan="4">No runs yet. Start one in Bench Studio or use the commands below.</td></tr>'
    )
    page = (
        """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <meta http-equiv="refresh" content="15"><title>BetterBench · Historical index</title>
    <style>:root{color-scheme:light dark;font-family:system-ui,sans-serif}body{max-width:1200px;margin:40px auto;padding:0 24px}h1{margin-bottom:8px}p{line-height:1.6;color:light-dark(#495263,#b8c1d0)}table{width:100%;border-collapse:collapse;margin:26px 0}th,td{text-align:left;vertical-align:top;padding:14px 12px;border-bottom:1px solid #8885}small{display:block;margin-top:7px;max-width:470px;overflow-wrap:anywhere;opacity:.8}a{color:light-dark(#1756bf,#9dbdff)}code,pre{font-family:ui-monospace,monospace;font-size:13px}pre{padding:18px;background:#8881;border:1px solid #8884;border-radius:8px;overflow:auto}.good{color:light-dark(#137443,#74d49f)}.bad{color:light-dark(#b02929,#ff9999)}.active{color:light-dark(#73530c,#f5d485)}@media(max-width:700px){body{padding:0 10px}th,td{padding:10px 5px}code{font-size:11px}}</style>
    <h1>BetterBench</h1><p>Local inference performance. Reports measure speed and latency, not answer correctness.
    Smoke runs are installation checks. The page refreshes every 15 seconds; use Bench Studio or its CLI for live status, logs and stopping.</p>
    <table><thead><tr><th>Run</th><th>Workload</th><th>Status</th><th>Results</th></tr></thead><tbody>"""
        + body
        + """</tbody></table>
    <h2>Run a benchmark</h2><pre>./bench models
    ./bench run daytime --profile coding
    ./bench run nighttime --profile standard
    ./bench run both --profile smoke
    ./bench run both --profile coding --parallel
    ./bench status
    ./bench logs RUN_ID
    ./bench stop RUN_ID</pre>
    <p>Profiles: smoke, coding, standard, prefill, prefill-smoke. Concurrency is one request per model. Each run resolves the current AI Runtime models.
    An interrupted phase may have logs without a report; completed phases remain available.</p></html>"""
    )
    return page


def write_index(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".index.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        atomic_text(root / "index.html", render_index(root))
