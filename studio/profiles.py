import hashlib
import json
from . import config, db

SPEED = {
    "smoke": ("Quick smoke", "A short routed-inference check."),
    "coding": ("Coding speed", "Code generation, file edits, and structured output."),
    "standard": ("Everyday mix", "Eight common assistant workloads."),
    "prefill": ("Context sweep", "Prompt processing across five input depths."),
    "prefill-smoke": ("Context smoke", "A short two-depth prefill check."),
}


def fingerprint(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def builtins():
    rows = []
    for key, (name, description) in SPEED.items():
        spec = json.loads((config.ROOT / "profiles" / f"{key}.json").read_text())
        rows.append(
            {
                "id": key,
                "name": name,
                "description": description,
                "family": "speed",
                "engine": "BetterBench 0.6.0",
                "version": 1,
                "builtin": True,
                "spec": spec,
                "parameters": {
                    "temperature": spec["config"]["temperature"],
                    "top_p": spec["config"].get("top_p", 1),
                    "seed": 42,
                    "reasoning_effort": "default",
                    "max_tokens": None,
                },
                "sizes": ["standard"],
            }
        )
    for key, name, languages in [
        ("coding-checks", "Coding checks", ["python", "typescript"]),
        ("python-checks", "Python checks", ["python"]),
        ("typescript-checks", "TypeScript checks", ["typescript"]),
    ]:
        rows.append(
            {
                "id": key,
                "name": name,
                "description": "Solutions graded by executable tests; one attempt per task.",
                "family": "quality",
                "engine": "EvalPlus 0.3.1 + MultiPL-E",
                "version": 2,
                "builtin": True,
                "languages": languages,
                "parameters": {
                    "temperature": 0,
                    "top_p": 1,
                    "seed": 42,
                    "reasoning_effort": "default",
                    "max_tokens": 8192,
                    "reasoning_budget_tokens": None,
                },
                "sizes": ["quick", "standard", "full"],
            }
        )
    rows.append(
        {
            "id": "repository-tasks",
            "name": "Repository tasks",
            "description": "Real Python and TypeScript repository issues. Fixed local SWE-bench Pro subset.",
            "family": "agent",
            "engine": "Harbor / Terminus 2",
            "version": 2,
            "builtin": True,
            "languages": ["python", "typescript"],
            "parameters": {
                "temperature": 0,
                "top_p": 1,
                "seed": 42,
                "reasoning_effort": "default",
                "max_tokens": 8192,
                "reasoning_budget_tokens": None,
                "max_turns": 40,
                "task_timeout": 1800,
            },
            "sizes": ["quick", "standard", "full"],
        }
    )
    for p in rows:
        p["fingerprint"] = fingerprint(p)
    return rows


def all_profiles():
    with db.connect() as c:
        custom = [db.unpack(r) for r in c.execute("SELECT document FROM profiles")]
    return builtins() + custom


def get(pid):
    p = next((x for x in all_profiles() if x["id"] == pid), None)
    if p is None:
        raise ValueError("Unknown profile")
    return p


def configure(pid, size="standard", overrides=None):
    p = get(pid)
    if size not in p["sizes"]:
        raise ValueError("Unsupported test size")
    p["size"] = size
    overrides = overrides or {}
    if set(overrides) - set(p["parameters"]):
        raise ValueError("Unsupported parameter")
    params = p["parameters"] | overrides
    for key, value in params.items():
        if key == "reasoning_effort":
            if value not in ["default", "off", "low", "medium", "xhigh"]:
                raise ValueError("Invalid reasoning effort")
        elif value is None and (key == "reasoning_budget_tokens" or (key == "max_tokens" and p["family"] == "speed")):
            pass
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Invalid {key}")
        elif key == "temperature" and not 0 <= value <= 2:
            raise ValueError("Temperature must be 0–2")
        elif key == "top_p" and not 0 < value <= 1:
            raise ValueError("top_p must be >0 and ≤1")
        elif key in ["max_tokens", "max_turns", "task_timeout", "seed", "reasoning_budget_tokens"]:
            if not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
            if key != "seed" and value <= 0:
                raise ValueError(f"{key} must be positive")
    if params.get("max_tokens") and params["max_tokens"] > 65536:
        raise ValueError("Maximum output limit is 65,536")
    thinking = params.get("reasoning_budget_tokens")
    if thinking is not None and (thinking >= params["max_tokens"] or params["reasoning_effort"] == "off"):
        raise ValueError("Reasoning budget requires reasoning enabled and must leave room within the total output limit for an answer")
    if params.get("max_turns", 1) > 100 or params.get("task_timeout", 1) > 3600:
        raise ValueError("Agent limit exceeds allowed budget")
    if "prefill" in p.get("spec", {}).get("phases", []) and params["temperature"] != 0:
        raise ValueError("BetterBench prefill fixes temperature at zero")
    if not -(2**31) <= params["seed"] < 2**31:
        raise ValueError("Seed must fit a signed 32-bit integer")
    p["parameters"] = params
    p["fingerprint"] = fingerprint({k: v for k, v in p.items() if k != "fingerprint"})
    return p


def attach_manifest(p):
    if p["family"] in ["quality", "agent"]:
        p["execution_adapter_version"] = 2
    if p["family"] == "quality":
        dataset = json.loads(
            (config.ROOT / "datasets/coding-manifest.json").read_text()
        )
        p["task_manifest_hash"] = fingerprint(
            {
                "dataset": dataset,
                "languages": p["languages"],
                "size": p["size"],
                "selection": "SHA256(42:task_id)",
            }
        )
        p["dataset_versions"] = dataset
    elif p["family"] == "agent":
        path = config.DATA / "repository-manifest.json"
        catalog = json.loads(path.read_text()) if path.exists() else {}
        count = {"quick": 2, "standard": 5, "full": 20}[p["size"]]
        tasks = catalog.get("tasks", [])[:count]
        if len(tasks) != count or not all(t.get("passed") for t in tasks):
            raise ValueError(
                "Repository tasks are not ready; reference-solution validation is required"
            )
        p["task_manifest_hash"] = fingerprint(
            [
                {
                    "id": t["id"],
                    "base_commit": t["base_commit"],
                    "image_id": t["image_id"],
                }
                for t in tasks
            ]
        )
        p["task_ids"] = [t["id"] for t in tasks]
        p["dataset_version"] = catalog["dataset_revision"]
        p["engine"] = "Harbor 0.23.0 / Terminus 2"
    p["fingerprint"] = fingerprint({k: v for k, v in p.items() if k != "fingerprint"})
    return p
