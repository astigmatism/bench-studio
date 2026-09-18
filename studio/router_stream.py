"""Strict OpenAI-compatible SSE collection for trusted execution adapters."""

import json, time
import httpx


class ContextBudgetError(RuntimeError):
    """The router rejected the input before generation because it cannot fit."""


async def completion(endpoint, payload, *, transport=None, progress=None):
    payload = dict(payload, stream=True, stream_options={"include_usage": True})
    content = []
    reasoning = []
    usage = None
    finish = None
    done = False
    started = last = time.monotonic()
    first_token = None
    async with httpx.AsyncClient(
        transport=transport, timeout=httpx.Timeout(600, connect=15)
    ) as client:
        async with client.stream(
            "POST", endpoint.rstrip("/") + "/chat/completions", json=payload
        ) as response:
            if response.is_error:
                await response.aread()
                try:
                    error = response.json().get("error", {})
                except (ValueError, AttributeError):
                    error = {}
                if (
                    isinstance(error, dict)
                    and error.get("code") == "context_length_exceeded"
                ):
                    raise ContextBudgetError(
                        error.get("message", "Input context exhausted")
                    )
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                value = line[5:].strip()
                if value == "[DONE]":
                    done = True
                    break
                chunk = json.loads(value)
                if chunk.get("error"):
                    if (
                        isinstance(chunk["error"], dict)
                        and chunk["error"].get("code") == "context_length_exceeded"
                        and not (content or reasoning)
                    ):
                        raise ContextBudgetError(
                            chunk["error"].get("message", "Input context exhausted")
                        )
                    raise RuntimeError("Router error: " + str(chunk["error"]))
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices", []):
                    finish = choice.get("finish_reason") or finish
                    delta = choice.get("delta", {})
                    if first_token is None and (
                        delta.get("content") or delta.get("reasoning_content")
                    ):
                        first_token = time.monotonic()
                    if delta.get("content"):
                        content.append(delta["content"])
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                if progress and time.monotonic() - last > 5:
                    progress(len("".join(content)) + len("".join(reasoning)))
                    last = time.monotonic()
    if (
        not done
        or finish not in ["stop", "length"]
        or not usage
        or any(
            type(usage.get(k)) is not int or usage[k] <= 0
            for k in ["prompt_tokens", "completion_tokens"]
        )
    ):
        raise RuntimeError("Incomplete router stream or missing real token usage")
    return {
        "content": "".join(content),
        "reasoning_content": "".join(reasoning),
        "usage": usage,
        "finish_reason": finish,
        "elapsed_seconds": time.monotonic() - started,
        "ttft_ms": (first_token - started) * 1000 if first_token is not None else None,
    }
