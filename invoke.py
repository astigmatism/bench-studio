"""Run the pinned upstream CLI with request-level validation and progress logging.

Timing and report calculations remain entirely in upstream BetterBench. The hook
validates each returned sample, including warmups/prefill, before aggregation can
discard errors. It does no additional work inside the measured HTTP interval.

Before each request the hook confirms the run's pinned model is unchanged
(LLM Router client contract §3). A router switch ends the phase: the worker
excludes the phase, waits for the switch, and runs the phase again. Failures
are classified by the router's `error.code` and recorded for the worker.
"""

import asyncio
import os
import json

from betterbench import runner
from betterbench.cli import main
from common import (
    atomic_json,
    error_kind,
    error_message,
    now,
    read_json,
    router_error,
    valid_sample,
    wait_for_runtime,
)

original = runner.stream_chat
counter = 0


def gate():
    """Readiness and identity before a new request; never waits out a switch."""
    path = os.environ.get("BB_GATE_FILE")
    if not path:
        return
    gate = read_json(path)
    wait_for_runtime(
        gate["settings"], gate["target"], gate["baseline"], wait_for_switch=False
    )


def classify(result):
    """The benchmark exception for a failed sample that carries a router code."""
    if not result.error_code:
        return None
    status = None
    if result.error and result.error.startswith("HTTP "):
        try:
            status = int(result.error.split()[1].rstrip(":"))
        except (IndexError, ValueError):
            status = None
    body = {"error": {"code": result.error_code, "message": result.error}}
    return router_error(
        status, body, target=os.environ.get("BB_TARGET"), stream=status is None
    )


async def checked_chat(*args, **kwargs):
    global counter
    counter += 1
    number = counter
    category = kwargs.get("category", "request")
    prompt = kwargs.get("prompt_id", "")
    progress = {
        "request": number,
        "category": category,
        "prompt": prompt,
        "state": "running",
        "started_at": now(),
    }
    progress_file = os.environ.get("BB_PROGRESS_FILE")
    if progress_file:
        atomic_json(progress_file, progress)
    # Outside the measured interval: stream_chat starts its clock on send.
    await asyncio.to_thread(gate)
    print(f"[request {number}] {category}/{prompt} started", flush=True)
    extra = json.loads(os.environ.get("BB_EXTRA_BODY", "{}"))
    if extra:
        kwargs["extra_body"] = dict(kwargs.get("extra_body") or {}, **extra)
    if os.environ.get("BB_MAX_TOKENS"):
        kwargs["max_tokens"] = int(os.environ["BB_MAX_TOKENS"])
    result = await original(*args, **kwargs)
    failure = classify(result)
    if failure:
        raise failure
    row = result.as_dict()
    valid_sample(row)
    progress.update(
        state="completed",
        finished_at=now(),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        finish_reason=result.finish_reason,
    )
    if progress_file:
        atomic_json(progress_file, progress)
    print(
        f"[request {number}] {category}/{prompt} done: input={result.prompt_tokens} output={result.completion_tokens} "
        f"TTFT={result.ttft_ms:.1f}ms decode={result.decode_tps:.2f}tok/s finish={result.finish_reason}",
        flush=True,
    )
    return result


def record_failure(exc):
    """Leave the classification for the worker; the exit status alone loses it."""
    path = os.environ.get("BB_FAILURE_FILE")
    if path and error_kind(exc):
        atomic_json(path, {"kind": error_kind(exc), "message": error_message(exc)})


if __name__ == "__main__":
    runner.stream_chat = checked_chat
    try:
        main()
    except Exception as exc:
        record_failure(exc)
        raise
