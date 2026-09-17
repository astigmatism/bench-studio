#!/usr/bin/env python3
"""Check the actual Harbor environment adapter with an upstream oracle agent (no LLM)."""

import asyncio, json, sys, uuid
from pathlib import Path
from harbor.job import Job
from harbor.models.job.config import JobConfig
from studio import config


async def main():
    catalog = json.loads((config.DATA / "repository-manifest.json").read_text())
    task = catalog["tasks"][0]
    name = "harbor-oracle-" + uuid.uuid4().hex[:8]
    cfg = JobConfig.model_validate(
        {
            "job_name": name,
            "jobs_dir": str(config.DATA / "validation"),
            "n_concurrent_trials": 1,
            "n_attempts": 1,
            "retry": {"max_retries": 0},
            "environment": {
                "import_path": "studio.harbor_environment:IsolatedDocker",
                "kwargs": {"run_id": name},
                "override_cpus": 2,
                "override_memory_mb": 8192,
                "override_gpus": 0,
            },
            "agents": [{"name": "oracle"}],
            "tasks": [{"path": str(config.DATA / "repository-tasks" / task["id"])}],
        }
    )
    job = await Job.create(cfg)
    await job.run()
    result = list((config.DATA / "validation" / name).glob("*/result.json"))
    if len(result) != 1:
        raise RuntimeError("Missing trial result")
    r = json.loads(result[0].read_text())
    print(json.dumps(r.get("exception_info") or r.get("verifier_result"), indent=2))
    assert (
        not r.get("exception_info") and r["verifier_result"]["rewards"]["reward"] == 1
    )
    print("Harbor isolated oracle PASS:", name)


asyncio.run(main())
