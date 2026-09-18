"""Task snapshots and partial Harbor evidence, without importing the harness."""

import hashlib
import json
import re
from pathlib import Path

DEFINITION_PATHS = ("task.toml", "instruction.md", "environment", "tests", "solution")
BUDGET_ERRORS = {"AgentTimeoutError", "OutputLengthExceededError", "ContextLengthExceededError"}


def snapshot_task(source, destination):
    """Copy only the task definition; oracle logs are never benchmark inputs."""
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name in DEFINITION_PATHS:
        src = source / name
        if not src.exists():
            raise RuntimeError(f"Repository task missing required definition: {src}")
        if src.is_symlink():
            raise RuntimeError(f"Repository task definition cannot be a symlink: {src}")
        files = sorted(src.rglob("*")) if src.is_dir() else [src]
        for path in files:
            if path.is_symlink():
                raise RuntimeError(f"Repository task definition cannot be a symlink: {path}")
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            data = path.read_bytes()  # Preflight every byte before making any LLM call.
            dest = destination / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            dest.chmod(0o755 if path.suffix == ".sh" else 0o644)
            hashes[str(relative)] = hashlib.sha256(data).hexdigest()
        if src.is_dir():
            (destination / name).mkdir(exist_ok=True)
    return hashes


def collect_trials(out, expected, *, job_error=None):
    out = Path(out)
    if job_error and "unhandled errors in a TaskGroup" in job_error:
        log = out.parent / "run.log"
        if log.exists():
            with log.open("rb") as f:
                f.seek(max(0, log.stat().st_size - 64000))
                text = f.read().decode(errors="replace")
            causes = re.findall(r"(?:PermissionError|FileNotFoundError|RuntimeError|ValueError): [^\n]+", text)
            if causes:
                job_error = causes[-1]
    by_id = {}
    errors = [job_error] if job_error else []
    for path in sorted(out.glob("harbor/*/*/result.json")):
        try:
            r = json.loads(path.read_text())
            task_id = (r.get("task_id") or {}).get("path", "").rstrip("/").split("/")[-1]
            if task_id not in {t["id"] for t in expected}:
                task_id = (r.get("task_name") or "").removeprefix("bench-studio/")
            if task_id not in {t["id"] for t in expected} or task_id in by_id:
                raise ValueError("Unexpected or duplicate Harbor task result: " + task_id)
            error = r.get("exception_info") or {}
            reward = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward")
            error_type = error.get("exception_type")
            infra = bool(error and error_type not in BUDGET_ERRORS)
            if reward not in (0, 1) and error_type not in BUDGET_ERRORS:
                infra = True
            message = error.get("exception_message") or error_type
            if infra:
                message = message or "Verifier did not produce a valid reward"
                errors.append(task_id + ": " + message)
            passed = reward == 1 and not error
            by_id[task_id] = {
                "id": task_id, "status": "infrastructure_error" if infra else "passed" if passed else "failed",
                "detail": message or ("Required tests passed" if passed else "Required tests failed"),
                "failure_kind": "infrastructure_error" if infra else "agent_budget" if error_type in BUDGET_ERRORS else None if passed else "test_failure",
                "reward": reward, "trial_path": str(path.relative_to(out)),
            }
        except (OSError, ValueError, TypeError) as exc:
            errors.append(str(exc))
    rows = []
    for task in expected:
        row = by_id.get(task["id"], {"id": task["id"], "status": "not_completed", "detail": "No completed trial; see run error and logs", "failure_kind": "not_completed"})
        rows.append(dict(row, language=task.get("language", "unknown")))
    completed = sum(r["status"] in ("passed", "failed") for r in rows)
    passed = sum(r["status"] == "passed" for r in rows)
    partial = bool(errors) or completed != len(expected)
    return {
        "score": None if partial or not rows else 100 * passed / len(rows),
        "unit": "%", "metric": "resolved_rate", "passed": passed,
        "count": len(rows), "completed_count": completed, "partial": partial,
        "tasks": rows, "infrastructure_error": "; ".join(errors)[:4000] if errors else None,
        "metrics": [{"language": lang, "passed": sum(r["status"] == "passed" for r in rows if r["language"] == lang),
                     "count": sum(r["language"] == lang for r in rows),
                     "rate": None if partial else 100 * sum(r["status"] == "passed" for r in rows if r["language"] == lang) / sum(r["language"] == lang for r in rows)}
                    for lang in sorted({r["language"] for r in rows})],
    }
