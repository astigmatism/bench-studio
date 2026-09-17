"""Run the pinned upstream CLI with request-level validation and progress logging.

Timing and report calculations remain entirely in upstream BetterBench. The hook
validates each returned sample, including warmups/prefill, before aggregation can
discard errors. It does no additional work inside the measured HTTP interval.
"""

import os
import json

from betterbench import runner
from betterbench.cli import main
from common import atomic_json, now, valid_sample

original = runner.stream_chat
counter = 0


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
    print(f"[request {number}] {category}/{prompt} started", flush=True)
    extra = json.loads(os.environ.get("BB_EXTRA_BODY", "{}"))
    if extra:
        kwargs["extra_body"] = dict(kwargs.get("extra_body") or {}, **extra)
    if os.environ.get("BB_MAX_TOKENS"):
        kwargs["max_tokens"] = int(os.environ["BB_MAX_TOKENS"])
    result = await original(*args, **kwargs)
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


if __name__ == "__main__":
    runner.stream_chat = checked_chat
    main()
