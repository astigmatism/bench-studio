"""Discovery, validation and static report indexing; no generation or Docker calls."""

from __future__ import annotations

import fcntl
import html
import hashlib
import http.client
import re
import json
import math
import os
import tempfile
import time
import urllib.request
from urllib.error import URLError
from datetime import datetime, timezone
from pathlib import Path

ACTIVE = {"starting", "running", "stopping"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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


CLIENT_NAME = "bench-studio"
# LLM Router client contract §7: while the router drains for a configuration
# switch or maintenance, keep waiting for at least ten minutes.
ROUTER_SWITCH_WAIT = 600
# A backend health failure (not a switch) gets a short grace period.
HEALTH_GRACE = 90
# Schema versions this client was written for (contract §11).
SCHEMA_VERSIONS = {"capabilities": 1, "metadata": 2, "capability_score": 1}


def client_name(run_id=None):
    """X-Client-Name value: per-run for benchmark traffic, plain for discovery."""
    return f"{CLIENT_NAME}/{run_id}" if run_id else CLIENT_NAME


def client_headers(run_id=None):
    return {"X-Client-Name": client_name(run_id)}


def router_url(settings):
    """Router API root (no /v1), e.g. http://192.168.1.4:11434."""
    explicit = settings.get("router_url")
    if explicit:
        return explicit.rstrip("/")
    endpoint = settings["endpoint"].rstrip("/")
    return endpoint[: -len("/v1")] if endpoint.endswith("/v1") else endpoint


def get_json(url, headers=None, timeout=10):
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def fetch_capabilities(settings):
    """The router's deployment document, with per-model load (contract §4)."""
    return get_json(
        router_url(settings) + "/v1/router/capabilities?include=load",
        client_headers(),
    )


def snapshot(settings):
    # AI Runtime status stays: the router does not publish container IDs, image
    # IDs or restart counts, which check_drift needs.
    return {
        "observed_at": now(),
        "runtime": get_json(settings["runtime_url"]),
        "capabilities": fetch_capabilities(settings),
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


class ModelOffline(RuntimeUnavailable):
    """The current configuration deliberately stops the target (SERVICE_OFFLINE).

    Not a configuration change and never a reason to use another model: queued
    runs wait, running runs end as failed (their identity did not change).
    """

    def __init__(self, message, *, target=None, configuration=None, reason=None):
        super().__init__(message)
        self.target, self.configuration, self.reason = target, configuration, reason

    @property
    def failure(self):
        text = str(self)
        if text.startswith("model offline"):
            return text
        return f"model offline ({self.configuration or 'unknown configuration'}): {text}"

    @property
    def waiting(self):
        where = self.configuration or "the current configuration"
        return f"waiting: {self.target or 'model'} offline in {where}"


class RouterSwitching(RuntimeUnavailable):
    """The router is draining for a configuration switch or maintenance.

    Wait (at least ROUTER_SWITCH_WAIT) and resolve again; never switch models.
    `rejected` marks a request the router refused before admission, which
    therefore produced nothing and may be sent again after the wait.
    """

    def __init__(self, message, *, rejected=False):
        super().__init__(message)
        self.rejected = rejected


class ModelConfigurationChanged(RuntimeError):
    """The advertised model or its runtime identity no longer matches a run."""


class BenchmarkItemError(RuntimeError):
    """The router rejected one benchmark item (for example context_length_exceeded).

    The item fails; the run's model identity is intact.
    """

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


# Cross-process classification: workers record a kind, the runner re-raises it.
ERROR_KINDS = {
    "configuration_changed": ModelConfigurationChanged,
    "model_offline": ModelOffline,
    "router_switching": RouterSwitching,
    "runtime_unavailable": RuntimeUnavailable,
    "item_error": BenchmarkItemError,
}


def error_kind(exc):
    for kind in ("model_offline", "router_switching", "configuration_changed",
                 "item_error", "runtime_unavailable"):
        if isinstance(exc, ERROR_KINDS[kind]):
            return kind
    return None


def error_message(exc):
    """The text a run records for an exception."""
    return exc.failure if isinstance(exc, ModelOffline) else str(exc)


def classified(kind, message):
    """Rebuild a worker's classified failure in the controller."""
    return ERROR_KINDS.get(kind, RuntimeError)(message)


def router_error_code(body):
    """`error.code` from a router error body (dict, JSON text or bytes), or None.

    Tolerates legacy string errors; inside Ollama-style streams the code is in
    `x_router.stop_reason` of an incomplete frame (contract §9-§10).
    """
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict) and error.get("code"):
        return str(error["code"])
    router = body.get("x_router")
    if (
        isinstance(router, dict)
        and router.get("stop_reason")
        and (error or router.get("status") == "incomplete")
    ):
        return str(router["stop_reason"])
    return None


def _error_text(body):
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return body[:300]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "")[:300]
    return str(error or "")[:300]


def router_error(status, body, *, target=None, pinned=True, stream=False):
    """Classify a failed router response by `error.code`, then status (contract §10).

    `status` is the HTTP status of a rejected request, or None for a network
    failure; `stream=True` means an error frame inside an accepted stream.
    Returns an exception to raise. A benchmark never falls back: offline and
    draining become waits, a missing pinned model is a configuration change.
    """
    code = router_error_code(body)
    text = _error_text(body)
    where = "Router error in stream" if stream else f"HTTP {status}" if status else "network"
    detail = f"{where} {code or 'error'}: {text}".rstrip(": ")
    name = target or "model"
    if code == "SERVICE_OFFLINE":
        return ModelOffline(f"{name} is offline in the current runtime configuration ({detail})", target=target)
    if code == "MODEL_NOT_FOUND":
        if pinned:
            return ModelConfigurationChanged(
                f"Model/runtime configuration changed during the run: pinned model {name} is no longer offered ({detail})"
            )
        return RuntimeError(f"Router does not offer {name} ({detail})")
    if code in ("BACKEND_DRAINING", "MAINTENANCE_MODE"):
        # A rejected request was never admitted, so it produced nothing.
        return RouterSwitching(
            f"router switching configuration ({detail})",
            rejected=bool(status) and not stream,
        )
    if code == "BACKEND_UNAVAILABLE":
        return RuntimeUnavailable(f"Backend for {name} is unavailable ({detail})")
    if code == "context_length_exceeded":
        return BenchmarkItemError(f"Request does not fit the context window ({detail})", code=code)
    if stream:
        return RuntimeError(f"Response incomplete ({detail})")
    if status is None or status in (408, 429) or status >= 500:
        return RuntimeUnavailable(f"Transient router failure ({detail})")
    return RuntimeError(f"Router rejected the request ({detail})")


def capabilities(snap):
    return snap.get("capabilities") or {}


def configuration_id(snap):
    return (capabilities(snap).get("configuration") or {}).get("id")


def switching_reason(snap):
    """Why requests are paused right now, or None while the router accepts them."""
    router = capabilities(snap).get("router") or {}
    if router.get("accepting_requests") is False:
        if router.get("maintenance"):
            return "router in maintenance"
        reason = router.get("drain_reason")
        return "router switching configuration" + (f" ({reason})" if reason else "")
    if ((snap.get("runtime") or {}).get("maintenance") or {}).get("draining"):
        return "router switching configuration (AI Runtime reports draining)"
    return None


def schema_warnings(doc):
    """Warnings for schema versions this client was not written for (§11)."""
    warnings = []
    if doc.get("schema_version") not in (None, SCHEMA_VERSIONS["capabilities"]):
        warnings.append(f"capabilities schema_version {doc.get('schema_version')}")
    for model in doc.get("models") or []:
        meta = model.get("metadata") or {}
        if meta.get("schema_version") not in (None, SCHEMA_VERSIONS["metadata"]):
            warnings.append(f"{model.get('id')}: metadata schema_version {meta.get('schema_version')}")
        score = meta.get("capability_score")
        if isinstance(score, dict) and score.get("version") not in (None, SCHEMA_VERSIONS["capability_score"]):
            warnings.append(f"{model.get('id')}: capability_score version {score.get('version')}")
    return warnings


def find_model(caps, target):
    """The capabilities entry a service ID, alias or (pinned) canonical ID names."""
    models = caps.get("models") or []
    canonical = (caps.get("ids") or {}).get(target)
    for model in models:
        if canonical and model.get("id") == canonical:
            return model
    for model in models:
        if (
            target in (model.get("id"), model.get("service"))
            or target in (model.get("aliases") or [])
            or safe_target(model.get("id") or "") == target
        ):
            return model
    return None


def find_offline(caps, target, pinned=None):
    """The offline_services entry for a service ID or a pinned canonical ID."""
    for entry in caps.get("offline_services") or []:
        model = entry.get("model") or ""
        if (
            target == model
            or target in (entry.get("aliases") or [])
            or (model and safe_target(model) == target)
            or (pinned and pinned == model)
        ):
            return entry
    return None


def offline_error(snap, target, entry):
    configuration = configuration_id(snap) or "unknown"
    reason = entry.get("reason")
    return ModelOffline(
        f"{target} is offline in runtime configuration {configuration}"
        + (f" ({reason})" if reason else ""),
        target=target,
        configuration=configuration,
        reason=reason,
    )


def resolve(snap, target, *, require_healthy=True, pinned=None):
    """Resolve a service ID (or a run's legacy canonical target) to its identity.

    Never falls back: an absent target is offline (it is listed in
    offline_services) or a configuration change. While the router switches,
    no verdict is given; callers wait and resolve again.
    """
    switching = switching_reason(snap)
    if switching:
        raise RouterSwitching(switching)
    caps = capabilities(snap)
    model = find_model(caps, target)
    if model is None:
        entry = find_offline(caps, target, pinned)
        if entry:
            # No runtime service is expected for a deliberately stopped model.
            raise offline_error(snap, target, entry)
        raise ModelConfigurationChanged(
            f"Model/runtime configuration changed during the run: {target!r} is absent from discovery"
        )
    meta = model.get("metadata") or {}
    canonical = model.get("id") or meta.get("upstream_model")
    if not meta.get("complete"):
        # Incomplete metadata is a failed source, not a new model (§4); the
        # identity cannot be verified until it is complete again.
        raise RuntimeUnavailable(
            f"Model {target} has incomplete metadata; its identity cannot be verified"
        )
    available = model.get("available")
    if available is None:
        available = (meta.get("health") or {}).get("available")
    if require_healthy and not available:
        raise RuntimeUnavailable(f"Model {target} is temporarily unavailable")
    context = model.get("context_window", meta.get("context_window"))
    if not isinstance(context, int) or isinstance(context, bool) or context <= 0:
        raise ModelConfigurationChanged(f"Model {target} has no valid context window")
    runtime = snap.get("runtime") or {}
    service = next(
        (s for s in runtime.get("services", []) if s.get("model") == canonical),
        None,
    )
    if not service:
        raise ModelConfigurationChanged(f"Model/runtime configuration changed during the run: missing {target}: {canonical}")
    if require_healthy and (not service.get("healthy") or not service.get("running")):
        raise RuntimeUnavailable(f"Runtime has no healthy service for {target}: {canonical}")
    configuration = caps.get("configuration") or {}
    return {
        "alias": target,
        "canonical": canonical,
        "context": context,
        "reserve": meta.get("context_safety_reserve", 1024),
        "metadata": meta,
        "service": service,
        "runtime_revision": runtime.get("deployed_revision"),
        "runtime_profile": runtime.get("profile"),
        # Report facts only: none of these are part of the model identity.
        "router": {
            "service": model.get("service"),
            "aliases": model.get("aliases") or [],
            "display_name": model.get("display_name"),
            "configuration": configuration.get("id"),
            "exclusive": configuration.get("exclusive"),
            "capability_score": model.get("capability_score"),
            "nsfw": model.get("nsfw"),
            "slots": model.get("slots"),
            "revision": caps.get("revision"),
        },
    }


def identity(resolved, *, instance=True):
    """The facts a run's results belong to.

    The model identity (instance=False) must hold from queueing to the end of
    a run. The running instance (container) must also hold once requests start.
    The configuration ID, capability score and NSFW flag are not identity.
    """
    m, s = resolved["metadata"], resolved["service"]
    value = {
        "canonical": resolved["canonical"],
        "context": resolved["context"],
        "reserve": resolved["reserve"],
        "model_revision": m.get("revision"),
        "quantization": m.get("quantization"),
        "reasoning": m.get("reasoning"),
        "image_id": s.get("image_id"),
        "runtime_revision": resolved["runtime_revision"],
    }
    if instance:
        value["container_id"] = s.get("id")
    return value


def model_fingerprint(resolved):
    """Opaque, stable comparison key for a run's observed model identity."""
    payload = json.dumps(identity(resolved), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def model_load(snap, canonical):
    return ((capabilities(snap).get("load") or {}).get(canonical)) or {}


def require_idle(snap, targets, *, all_services=True):
    switching = switching_reason(snap)
    if switching:
        raise RouterSwitching(switching)
    runtime = snap["runtime"]
    if not runtime.get("ready"):
        raise RuntimeError("AI Runtime is not ready or is draining")
    maintenance = runtime.get("maintenance", {})
    if all_services and (
        maintenance.get("active_requests", 0) or maintenance.get("queued_requests", 0)
    ):
        raise RuntimeError("Router has existing active/queued work; retry when idle")
    resolved = [resolve(snap, t) for t in targets]
    services = runtime.get("services", []) if all_services else [r["service"] for r in resolved]
    if any(s.get("processing") for s in services):
        busy = ", ".join(s["name"] for s in services if s.get("processing"))
        raise RuntimeError(f"Backend processing is active ({busy}); retry when idle")
    for r in resolved:
        load = model_load(snap, r["canonical"])
        if load.get("active") or load.get("queued"):
            raise RuntimeError(
                f"Router has active/queued work for {r['alias']}; retry when idle"
            )


def check_drift(before, after, *, instance=True):
    service = after.get("service", {})
    if (identity(before, instance=instance) != identity(after, instance=instance)
        or service.get("differences")
        or (instance and any(before.get("service", {}).get(k) != service.get(k)
                             for k in ("started_at", "restart_count") if k in before.get("service", {})))):
        raise ModelConfigurationChanged(
            f"Model/runtime configuration changed during the run for {before.get('alias', before['canonical'])}"
        )


TRANSIENT = (URLError, TimeoutError, ConnectionError, http.client.HTTPException, json.JSONDecodeError)


def wait_for_runtime(settings, target, expected, *, timeout=HEALTH_GRACE,
                     switch_timeout=ROUTER_SWITCH_WAIT, on_wait=None,
                     wait_for_switch=True):
    """Wait before a new request; never retry an inference request.

    Health failures get `timeout`; a router switch gets at least ten minutes
    (contract §7). Afterwards the target is resolved again: an identity change
    raises ModelConfigurationChanged, an offline target ModelOffline. Neither
    ever selects another model. With wait_for_switch=False a switch raises
    RouterSwitching at once (the speed worker excludes that phase instead).
    """
    health_deadline = switch_deadline = None
    delay = 2
    while True:
        try:
            snap = snapshot(settings)
            pinned = expected.get("canonical")
            observed = resolve(snap, target, require_healthy=False, pinned=pinned)
            check_drift(expected, observed)
            return resolve(snap, target, pinned=pinned)
        except ModelOffline:
            raise
        except RouterSwitching as exc:
            if not wait_for_switch:
                raise
            health_deadline = None
            switch_deadline = switch_deadline or time.monotonic() + switch_timeout
            remaining = switch_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeUnavailable(
                    f"Router did not finish switching within {switch_timeout}s: {exc}"
                ) from exc
            if on_wait:
                on_wait(exc)
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 30)
        except (RuntimeUnavailable, *TRANSIENT) as exc:
            health_deadline = health_deadline or time.monotonic() + timeout
            if time.monotonic() >= health_deadline:
                raise RuntimeUnavailable(f"Runtime readiness did not recover within {timeout}s: {exc}") from exc
            if on_wait:
                on_wait(exc)
            time.sleep(min(3, max(0, health_deadline - time.monotonic())))


def send_when_ready(settings, target, expected, send, *, on_wait=None):
    """Gate a request on readiness; resend only what the router refused while draining.

    A BACKEND_DRAINING or MAINTENANCE_MODE rejection admitted nothing, so the
    same request is sent again once the switch ends (contract §7). Any other
    failure propagates: inference is never replayed.
    """
    first_rejection = None
    while True:
        wait_for_runtime(settings, target, expected, on_wait=on_wait)
        try:
            return send()
        except RouterSwitching as exc:
            if not exc.rejected:
                raise
            first_rejection = first_rejection or time.monotonic()
            if time.monotonic() - first_rejection >= ROUTER_SWITCH_WAIT:
                raise RuntimeUnavailable(
                    f"Router kept refusing requests for {ROUTER_SWITCH_WAIT}s: {exc}"
                ) from exc
            if on_wait:
                on_wait(exc)
            time.sleep(2)


# Router drain windows, written by the runner's subscriber (it sees every
# drain on the event stream) and read by speed workers: a measured phase that
# overlaps a drain is excluded and measured again.
DRAINS_FILE = "router-drains.json"


def drain_windows(root):
    try:
        windows = read_json(Path(root) / DRAINS_FILE)
    except (OSError, ValueError):
        return []
    return windows if isinstance(windows, list) else []


def note_router_state(root, accepting, reason=None, *, at=None):
    """Open or close a drain window; returns True when the record changed."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".router-drains.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        windows = drain_windows(root)
        draining = bool(windows) and windows[-1].get("ended_at") is None
        if not accepting and not draining:
            windows.append({"started_at": at or now(), "ended_at": None, "reason": reason})
        elif accepting and draining:
            windows[-1]["ended_at"] = at or now()
        else:
            return False
        atomic_json(root / DRAINS_FILE, windows[-200:])
        return True


def drains_overlapping(root, started_at, finished_at):
    """Drain windows that overlap [started_at, finished_at] (ISO timestamps)."""
    start = datetime.fromisoformat(started_at)
    end = datetime.fromisoformat(finished_at)
    found = []
    for window in drain_windows(root):
        try:
            began = datetime.fromisoformat(window["started_at"])
            ended = datetime.fromisoformat(window["ended_at"]) if window.get("ended_at") else None
        except (KeyError, TypeError, ValueError):
            continue
        if began <= end and (ended is None or ended >= start):
            found.append(window)
    return found


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
    <h1>BetterBench</h1><p>Router inference performance. Reports measure speed and latency, not answer correctness.
    Smoke runs are installation checks. The page refreshes every 15 seconds; use Bench Studio or its CLI for live status, logs and stopping.</p>
    <table><thead><tr><th>Run</th><th>Workload</th><th>Status</th><th>Results</th></tr></thead><tbody>"""
        + body
        + """</tbody></table>
    <h2>Run a benchmark</h2><pre>./bench models
    ./bench run MODEL_ALIAS --profile smoke
    ./bench run MODEL_ALIAS --profile coding
    ./bench status
    ./bench logs RUN_ID
    ./bench stop RUN_ID</pre>
    <p>Profiles: smoke, coding, standard, prefill, prefill-smoke. Each new run selects one current AI Runtime model.
    An interrupted phase may have logs without a report; completed phases remain available.</p></html>"""
    )
    return page


def write_index(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".index.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        atomic_text(root / "index.html", render_index(root))
