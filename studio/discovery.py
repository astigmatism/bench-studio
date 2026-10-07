"""Model discovery from the LLM Router capabilities document (client contract §4).

Each selectable model is offered by its service ID (`daytime`, `nighttime`).
A run starts from a service ID and pins the canonical model behind it for the
run's duration (contract §3, benchmark exception). Discovery never offers a
substitute for an offline or unavailable service.
"""

from common import (
    RuntimeUnavailable,
    capabilities,
    fetch_capabilities,
    get_json,
    identity,
    model_fingerprint,
    model_load,
    now,
    resolve,
    safe_target,
    schema_warnings,
    switching_reason,
)
from . import config
from .session_catalog import vision_support


def offline_services(caps):
    return [
        {
            "service": (entry.get("aliases") or [entry.get("model")])[0],
            "aliases": entry.get("aliases") or [],
            "model": entry.get("model"),
            "display_name": entry.get("display_name"),
            "reason": entry.get("reason"),
        }
        for entry in caps.get("offline_services") or []
    ]


def router_state(snap):
    caps = capabilities(snap)
    router = caps.get("router") or {}
    configuration = caps.get("configuration") or {}
    switching = switching_reason(snap)
    return {
        "revision": caps.get("revision"),
        "accepting_requests": router.get("accepting_requests") is not False,
        "switching": bool(switching),
        "switching_reason": switching,
        "maintenance": bool(router.get("maintenance")),
        "configuration": configuration.get("id"),
        "exclusive": configuration.get("exclusive"),
        "warnings": [*schema_warnings(caps), *(caps.get("warnings") or [])],
    }


def fetch(settings):
    """A snapshot that tolerates an unreachable AI Runtime status.

    Raises only when the router itself is unreachable.
    """
    caps = fetch_capabilities(settings)
    try:
        runtime, runtime_error = get_json(settings["runtime_url"]), None
    except Exception as exc:
        runtime, runtime_error = {}, f"AI Runtime status unavailable: {exc}"
    return {"observed_at": now(), "runtime": runtime, "capabilities": caps, "runtime_error": runtime_error}


def discover():
    snap = fetch(config.SETTINGS)
    caps = snap["capabilities"]
    runtime, runtime_error = snap["runtime"], snap.get("runtime_error")
    state = router_state(snap)
    models = []
    for row in caps.get("models") or []:
        meta = row.get("metadata") or {}
        service = row.get("service")
        placement = meta.get("placement") or {}
        load = model_load(snap, row.get("id"))
        base = {
            # Selection ID: the stable service ID, never the canonical ID.
            "alias": service or safe_target(row.get("id") or ""),
            "service": service,
            "canonical": row.get("id"),
            "display_name": row.get("display_name"),
            "configuration": state["configuration"],
            "capability_score": row.get("capability_score"),
            "nsfw": row.get("nsfw"),
            "slots": row.get("slots"),
            "context": row.get("context_window"),
            "gpus": placement.get("gpus") or [],
            "load": {k: load.get(k) for k in ("active", "queued", "free_slots") if k in load},
            "reasoning": meta.get("reasoning", {}),
            "vision": vision_support(meta),
        }
        try:
            if not service:
                raise RuntimeUnavailable(
                    "The router publishes no service ID for this model, so it cannot be selected"
                )
            if runtime_error:
                raise RuntimeUnavailable(runtime_error)
            resolved = resolve(snap, service)
            if not runtime.get("ready"):
                raise RuntimeUnavailable("AI Runtime is not ready")
            models.append(
                {
                    **base,
                    "available": True,
                    "context": resolved["context"],
                    "gpus": resolved["service"].get("gpu_names") or base["gpus"],
                    "processing": resolved["service"].get("processing", False)
                    or bool(load.get("active") or load.get("queued")),
                    "resolved": resolved,
                    "identity": identity(resolved),
                    "fingerprint": model_fingerprint(resolved),
                }
            )
        except RuntimeError as e:
            models.append({**base, "available": False, "error": str(e)})
    return {
        "models": models,
        "offline_services": offline_services(caps),
        "router": state,
        "runtime": runtime,
        "runtime_error": runtime_error,
        "snapshot": snap,
    }
