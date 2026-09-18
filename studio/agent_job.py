"""Run pinned local Harbor tasks with the selected canonical models."""

import asyncio, json, sys
from pathlib import Path
from common import read_json, atomic_json, now
from . import config
from .repository import collect_trials
from .diagnostics import exception_message


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
        if p.get("reasoning_budget_tokens") is not None:
            kwargs["llm_kwargs"]["reasoning_budget_tokens"] = p["reasoning_budget_tokens"]
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
                {"path": str(root / "repository-tasks" / r["id"])}
                for r in m["repository_tasks"]
            ],
        }
        atomic_json(out / "harbor-config.json", cfg)
        print(now(), t, "Starting", len(cfg["tasks"]), "repository tasks", flush=True)
        error = None
        try:
            job = await Job.create(JobConfig.model_validate(cfg))
            await job.run()
            from .harbor_agent import RouterLLM
            failures = RouterLLM.infrastructure_errors.get(resolved["canonical"], [])
            if failures:
                raise RuntimeError("Model transport failed: " + "; ".join(failures)[:2000])
        except BaseException as exc:
            error = exception_message(exc)
            raise
        finally:
            result = collect_trials(out, m["repository_tasks"], job_error=error)
            atomic_json(out / "result.json", result)
        if result["partial"]:
            raise RuntimeError(result["infrastructure_error"] or "Harbor did not produce all expected trial results")

    try:
        if m["mode"] == "parallel":
            await asyncio.gather(*(target(t) for t in m["requested_targets"]))
        else:
            for t in m["requested_targets"]:
                await target(t)
        atomic_json(root / "agent-outcome.json", {"status": "completed"})
    except BaseException as e:
        atomic_json(root / "agent-outcome.json", {"status": "failed", "error": exception_message(e)})
        raise


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
