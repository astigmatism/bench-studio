"""Run pinned local Harbor tasks with the selected canonical models."""

import asyncio, json, sys
from pathlib import Path
from common import read_json, atomic_json, now
from . import config


async def main(path):
    from harbor.job import Job
    from harbor.models.job.config import JobConfig

    m = read_json(path)
    root = Path(path).parent
    p = m["profile_spec"]["parameters"]

    async def target(t):
        resolved = m["resolved"][t]
        out = root / t
        out.mkdir(exist_ok=True)
        kwargs = {
            "api_base": m["settings"]["endpoint"],
            "temperature": p["temperature"],
            "max_turns": p["max_turns"],
            "use_responses_api": False,
            "record_terminal_session": False,
            "llm_kwargs": {
                "max_tokens": p["max_tokens"],
                "seed": p["seed"],
                "top_p": p["top_p"],
            },
            "model_info": {
                "max_input_tokens": resolved["context"] - resolved["reserve"],
                "max_output_tokens": p["max_tokens"],
                "input_cost_per_token": 0,
                "output_cost_per_token": 0,
                "litellm_provider": "openai",
                "mode": "chat",
            },
        }
        if p["reasoning_effort"] != "default":
            kwargs["reasoning_effort"] = (
                "none" if p["reasoning_effort"] == "off" else p["reasoning_effort"]
            )
        cfg = {
            "job_name": m["id"] + "-" + t,
            "jobs_dir": str(out / "harbor"),
            "n_attempts": 1,
            "n_concurrent_trials": 1,
            "retry": {"max_retries": 0},
            "environment": {
                "import_path": "studio.harbor_environment:IsolatedDocker",
                "delete": True,
                "override_cpus": 2,
                "override_memory_mb": 8192,
                "override_gpus": 0,
                "kwargs": {"run_id": m["id"]},
            },
            "agents": [
                {
                    "import_path": "studio.harbor_agent:RouterTerminus2",
                    "model_name": "openai/" + resolved["canonical"],
                    "override_timeout_sec": p["task_timeout"],
                    "kwargs": kwargs,
                }
            ],
            "tasks": [
                {"path": str(config.DATA / "repository-tasks" / r["id"])}
                for r in m["repository_tasks"]
            ],
        }
        atomic_json(out / "harbor-config.json", cfg)
        print(now(), t, "Starting", len(cfg["tasks"]), "repository tasks", flush=True)
        job = await Job.create(JobConfig.model_validate(cfg))
        await job.run()
        trials = []
        infrastructure = []
        for path in (out / "harbor" / cfg["job_name"]).glob("*/result.json"):
            r = read_json(path)
            error = r.get("exception_info")
            reward = (r.get("verifier_result") or {}).get("rewards") or {}
            value = reward.get("reward", 0)
            task_id = r.get("task_name") or path.parent.name
            match = next((x for x in m["repository_tasks"] if x["id"] in task_id), None)
            if error and error.get("exception_type") not in [
                "AgentTimeoutError",
                "OutputLengthExceededError",
                "ContextLengthExceededError",
            ]:
                infrastructure.append(error.get("exception_message") or str(error))
            trials.append(
                {
                    "id": task_id,
                    "language": match["language"] if match else "unknown",
                    "status": "passed" if value == 1 and not error else "failed",
                    "detail": (
                        error.get("exception_type")
                        if error
                        else "Required tests " + ("passed" if value == 1 else "failed")
                    ),
                    "reward": value,
                    "trial_path": str(path.relative_to(out)),
                }
            )
        if len(trials) != len(m["repository_tasks"]):
            raise RuntimeError("Harbor did not produce all expected trial results")
        if infrastructure:
            raise RuntimeError(
                "Repository infrastructure error: " + "; ".join(infrastructure)[:1500]
            )
        passed = sum(x["status"] == "passed" for x in trials)
        metrics = []
        for lang in ["python", "typescript"]:
            rows = [x for x in trials if x["language"] == lang]
            if rows:
                metrics.append(
                    {
                        "language": lang,
                        "rate": 100
                        * sum(x["status"] == "passed" for x in rows)
                        / len(rows),
                    }
                )
        atomic_json(
            out / "result.json",
            {
                "score": 100 * passed / len(trials),
                "unit": "%",
                "metric": "resolved_rate",
                "passed": passed,
                "count": len(trials),
                "tasks": trials,
                "metrics": metrics,
            },
        )

    try:
        if m["mode"] == "parallel":
            await asyncio.gather(*(target(t) for t in m["requested_targets"]))
        else:
            for t in m["requested_targets"]:
                await target(t)
        atomic_json(root / "agent-outcome.json", {"status": "completed"})
    except BaseException as e:
        atomic_json(root / "agent-outcome.json", {"status": "failed", "error": str(e)})
        raise


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
