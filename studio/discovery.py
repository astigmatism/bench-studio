from common import RuntimeUnavailable, snapshot, resolve, identity, model_fingerprint, safe_target
from . import config
from .session_catalog import vision_support


def discover():
    snap = snapshot(config.SETTINGS)
    canonical = {}
    for row in snap["models"].get("data", []):
        meta = row.get("x_ollama_router", {})
        target = meta.get("upstream_model") or row["id"]
        canonical.setdefault(target, []).append(row)
    models = []
    for canonical_id, rows in canonical.items():
        # The router may expose several aliases for one backend. Prefer its
        # canonical row so changing an alias cannot silently retarget a rerun.
        row = next(
            (r for r in rows if r["id"] == canonical_id),
            next((r for r in rows if r["id"] != "local-active"), rows[0]),
        )
        alias = safe_target(row["id"])
        try:
            resolved = resolve(snap, alias)
            if not snap["runtime"].get("ready") or snap["runtime"].get(
                "maintenance", {}
            ).get("draining"):
                raise RuntimeUnavailable("AI Runtime is not ready or is draining")
            models.append(
                {
                    "alias": alias,
                    "canonical": canonical_id,
                    "available": True,
                    "context": resolved["context"],
                    "gpus": resolved["service"].get("gpu_names", []),
                    "processing": resolved["service"].get("processing", False),
                    "reasoning": resolved["metadata"].get("reasoning", {}),
                    "vision": vision_support(resolved["metadata"]),
                    "resolved": resolved,
                    "identity": identity(resolved),
                    "fingerprint": model_fingerprint(resolved),
                }
            )
        except RuntimeError as e:
            models.append(
                {
                    "alias": alias,
                    "canonical": canonical_id,
                    "available": False,
                    "error": str(e),
                }
            )
    return {"models": models, "runtime": snap["runtime"], "snapshot": snap}
