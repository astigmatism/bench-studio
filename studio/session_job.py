"""Trusted session controller subprocess. Model-written code only executes in Harbor."""

import asyncio
import base64
import signal
import os
import contextlib
import sys
import time
from pathlib import Path
from common import atomic_json, now, read_json, wait_for_runtime
from . import db
from .session_catalog import tasks
from .session_reviews import create, reviews
from .session_core import SessionCore, parse_json
from .session_environment import SessionEnvironment, verify_candidate
from .router_stream import completion, ContextBudgetError
from .session_results import summarize_attempts


async def run(manifest_path):
    m = read_json(manifest_path)
    root = Path(manifest_path).parent
    p = m["profile_spec"]
    latest_progress = {}

    def progress(value):
        if (value.get("target"), value.get("attempt_id")) != (
            latest_progress.get("target"),
            latest_progress.get("attempt_id"),
        ):
            latest_progress.clear()
        latest_progress.update(value, updated_at=now())
        atomic_json(root / "session-progress.json", latest_progress)

    async def ready(target):
        current = db.get_run(m["id"])
        if (
            not current
            or current["status"] in db.TERMINAL
            or current.get("cancel_requested")
        ):
            raise asyncio.CancelledError()
        await asyncio.to_thread(
            wait_for_runtime, m["settings"], target, m["resolved"][target]
        )

    async def review(target, attempt, revision, kind, artifact):
        value = create(m["id"], target, attempt, revision, kind, artifact)
        key = f"{target}:{attempt}:{revision}"
        progress(
            {
                "target": target,
                "attempt_id": attempt,
                "phase": "review_wait",
                "review_key": key,
            }
        )
        decided = None
        while True:
            current = db.get_run(m["id"])
            if (
                not current
                or current["status"] in db.TERMINAL
                or current.get("cancel_requested")
            ):
                raise asyncio.CancelledError()
            value = next(
                r
                for r in reviews(m["id"])
                if r["target"] == target
                and r["attempt_id"] == attempt
                and r["revision"] == revision
            )
            if value.get("decision") and decided is None:
                decided = time.monotonic()
            if value.get("decision") == "stop":
                raise asyncio.CancelledError()
            if (
                value.get("decision")
                and current.get("resume_review") == key
                and current["status"] == "running"
            ):
                return dict(value, resume_queue_seconds=time.monotonic() - decided)
            await asyncio.sleep(0.5)

    try:
        for target in m["requested_targets"]:
            out = root / target
            out.mkdir(exist_ok=True)
            rows = []
            expected = len(tasks(p)) * p["repetitions"]
            for task in tasks(p):
                for repetition in range(1, p["repetitions"] + 1):
                    attempt = f"{task['id']}-r{repetition}"
                    destination = out / attempt
                    destination.mkdir(exist_ok=True)
                    progress(
                        {
                            "target": target,
                            "attempt_id": attempt,
                            "phase": "preparation",
                        }
                    )
                    started = time.monotonic()
                    if p["family"] == "vision":
                        await ready(target)
                        image = root / "session-inputs" / task["image"]
                        content = [
                            {"type": "text", "text": task["prompt"]},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,"
                                    + base64.b64encode(image.read_bytes()).decode()
                                },
                            },
                        ]
                        params = {
                            k: p["parameters"][k]
                            for k in (
                                "temperature",
                                "top_p",
                                "seed",
                                "max_tokens",
                                "reasoning_budget_tokens",
                            )
                            if p["parameters"].get(k) is not None
                        }
                        effort = p["parameters"]["reasoning_effort"]
                        if effort != "default":
                            params["reasoning_effort"] = (
                                "none" if effort == "off" else effort
                            )
                        payload = {
                            "model": m["resolved"][target]["canonical"],
                            "messages": [{"role": "user", "content": content}],
                            **params,
                        }
                        atomic_json(destination / "request.json", payload)
                        progress(
                            {
                                "target": target,
                                "attempt_id": attempt,
                                "phase": "visual_review",
                            }
                        )
                        row = await vision_attempt(
                            m,
                            task,
                            attempt,
                            payload,
                            destination,
                            runtime_seconds=time.monotonic() - started,
                        )
                        row["artifact_root"] = str(destination.relative_to(root))
                        atomic_json(destination / "attempt.json", row)
                    else:
                        environment = SessionEnvironment(m, task, destination)
                        try:
                            await environment.start()
                            preparation = time.monotonic() - started

                            async def verify(number):
                                candidate = await environment.export(
                                    destination / f"candidate-{number}"
                                )
                                folder = destination / f"verification-{number}"
                                value = await verify_candidate(
                                    p["session_image"],
                                    candidate,
                                    folder,
                                    task,
                                    p["suite"],
                                    m["id"],
                                    checks_source=root / "session-inputs/acceptance",
                                )
                                value["artifact_root"] = str(folder.relative_to(root))
                                if p["suite"] == "visual-design":
                                    value["prototype_artifact"] = str(
                                        (folder / "prototype.html").relative_to(root)
                                    )
                                return value

                            core = SessionCore(
                                manifest=m,
                                target=target,
                                task=task,
                                attempt_id=attempt,
                                root=destination,
                                environment=environment,
                                review=review,
                                ready=lambda: ready(target),
                                progress=progress,
                                verify=verify,
                            )
                            from .session_agent import StudioSessionAgent
                            from harbor.models.agent.context import AgentContext

                            agent = StudioSessionAgent(
                                logs_dir=destination / "agent",
                                model_name="openai/"
                                + m["resolved"][target]["canonical"],
                                core=core,
                            )
                            row = await agent.run(
                                task["requirements"], environment.env, AgentContext()
                            )
                            row["preparation_seconds"] = preparation
                            row["phase_seconds"]["preparation"] = preparation
                            row["artifact_root"] = str(destination.relative_to(root))
                            atomic_json(destination / "attempt.json", row)
                        finally:
                            await environment.stop()
                    rows.append(row)
                    atomic_json(
                        out / "result.json", summarize_attempts(rows, p, expected)
                    )
                    if row["status"] == "infrastructure_error":
                        raise RuntimeError(
                            row.get("detail") or "Session infrastructure failed"
                        )
        atomic_json(root / "session-outcome.json", {"status": "completed"})
    except BaseException as exc:
        atomic_json(
            root / "session-outcome.json",
            {
                "status": "cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else "failed",
                "error": str(exc) or type(exc).__name__,
            },
        )
        raise


async def vision_attempt(
    manifest, task, attempt, payload, destination, *, runtime_seconds=0
):
    started = time.monotonic()
    row = {
        "id": attempt,
        "task_id": task["id"],
        "category": task["category"],
        "status": "infrastructure_error",
        "answer": None,
        "expected": task["answer"],
        "implementation_seconds": None,
        "requests": [],
        "runtime_wait_seconds": runtime_seconds,
        "image_dimensions": {"width": task.get("width"), "height": task.get("height")},
    }
    try:
        response = await asyncio.wait_for(
            completion(manifest["settings"]["endpoint"], payload), timeout=600
        )
        atomic_json(destination / "response.json", response)
        row["requests"] = [response]
        try:
            answer = parse_json(response["content"]).get("answer", "")
        except (ValueError, KeyError):
            answer = ""
        passed = (
            response["finish_reason"] == "stop"
            and isinstance(answer, str)
            and answer.strip().casefold() == task["answer"].casefold()
        )
        row.update(
            status="passed" if passed else "failed",
            answer=answer,
            failure_kind=None
            if passed
            else "output_limit"
            if response["finish_reason"] == "length"
            else "wrong_answer",
        )
    except asyncio.CancelledError:
        row.update(status="cancelled", failure_kind="cancelled")
        raise
    except ContextBudgetError as exc:
        row.update(status="failed", failure_kind="context_exhausted", detail=str(exc))
    except TimeoutError:
        row.update(
            status="failed",
            failure_kind="active_time_limit",
            detail="Vision request exceeded 600 seconds",
        )
    except Exception as exc:
        row.update(failure_kind="infrastructure", detail=str(exc))
    finally:
        row["active_seconds"] = time.monotonic() - started
        row["phase_seconds"] = {
            "visual_review": row["active_seconds"],
            "runtime_wait": runtime_seconds,
        }
        if not row["requests"]:
            row["requests"] = [
                {
                    "elapsed_seconds": row["active_seconds"],
                    "error": row.get("detail", row.get("failure_kind")),
                }
            ]
        atomic_json(destination / "attempt.json", row)
    return row


async def watch_controller(task, parent_pid):
    while not task.done():
        if os.getppid() != parent_pid:
            task.cancel("Controller interrupted; do not replay this session")
            return
        await asyncio.sleep(0.5)


async def main(path):
    task = asyncio.create_task(run(path))
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    watcher = asyncio.create_task(
        watch_controller(
            task, int(os.environ.get("STUDIO_CONTROLLER_PID", os.getppid()))
        )
    )
    try:
        await task
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
