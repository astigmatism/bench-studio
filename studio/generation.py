"""Record request settings and the observed defaults they override."""

import json
from . import config


def snapshot(run):
    profile = run["profile_spec"]
    requested = dict(profile["parameters"])
    sent = {
        k: requested[k]
        for k in ("temperature", "top_p", "seed", "max_tokens")
        if requested.get(k) is not None
    }
    if requested.get("reasoning_effort") != "default":
        sent["reasoning_effort"] = (
            "none"
            if requested["reasoning_effort"] == "off"
            else requested["reasoning_effort"]
        )
    budgets = {}
    if profile["family"] == "speed" and requested.get("max_tokens") is None:
        spec = profile["spec"]
        if "prefill" in spec["phases"]:
            budgets["prefill"] = spec["config"]["prefill_max_tokens"]
        if "decode" in spec["phases"]:
            for category in spec["categories"]:
                path = config.ROOT / "vendor/corpus/v1" / (category + ".jsonl")
                for line in path.read_text().splitlines():
                    row = json.loads(line)
                    budgets[row["id"]] = row["max_tokens"]
    result = {}
    for target in run["requested_targets"]:
        observed = run["host"]["backend_defaults"].get(target, {})
        defaults = observed.get("params", {})
        sampling_keys = {
            "seed",
            "temperature",
            "top_p",
            "top_k",
            "min_p",
            "typical_p",
            "repeat_penalty",
            "repeat_last_n",
            "presence_penalty",
            "frequency_penalty",
            "max_tokens",
        }
        effective = {k: v for k, v in defaults.items() if k in sampling_keys} | sent
        effective.pop("n_predict", None)
        if budgets:
            effective["max_tokens"] = "See output_budgets_by_workload"
        result[target] = {
            "requested": requested,
            "request_overrides": sent,
            "effective_sampling": effective,
            "output_budgets_by_workload": budgets,
            "reasoning": sent.get(
                "reasoning_effort",
                run["resolved"][target]
                .get("metadata", {})
                .get("reasoning", {})
                .get("default", "runtime default"),
            ),
            "basis": "Request overrides merged with backend defaults observed before execution; not a server echo. Per-workload output budgets apply where listed.",
            "backend_defaults_available": bool(defaults),
        }
    return result
