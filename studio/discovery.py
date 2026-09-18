from common import snapshot, resolve, identity, safe_target
from . import config
from .session_catalog import vision_support


def discover():
    snap = snapshot(config.SETTINGS)
    canonical = {}
    for row in snap["models"]["data"]:
        meta = row.get("x_ollama_router", {})
        target = meta.get("upstream_model") or row["id"]
        canonical.setdefault(target, []).append(row)
    models = []
    for canonical_id, rows in canonical.items():
        names = [r["id"] for r in rows]
        alias = next(
            (s for s in ["daytime", "nighttime"] if s in names),
            next(
                (
                    r["id"]
                    for r in rows
                    if r.get("x_ollama_router", {}).get("alias")
                    and r["id"] != "local-active"
                ),
                canonical_id,
            ),
        )
        alias = safe_target(alias)
        try:
            resolved = resolve(snap, alias)
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
