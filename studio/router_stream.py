"""Strict OpenAI-compatible SSE collection for trusted execution adapters."""

import json
import time
import httpx
from betterbench.telemetry import StreamTelemetry


class ContextBudgetError(RuntimeError):
    """The router rejected the input before generation because it cannot fit."""


class EmptyResponseError(RuntimeError):
    """Generation ended without an answer; partial evidence is not executable."""

    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence or {}


async def completion(endpoint, payload, *, transport=None, progress=None):
    payload = dict(payload, stream=True, stream_options={"include_usage": True})
    content = []
    reasoning = []
    usage = None
    finish = None
    done = False
    started = last = time.monotonic()
    telemetry = StreamTelemetry(started)

    def evidence():
        return {
            **telemetry.evidence(time.monotonic()),
            "content": "".join(content),
            "reasoning_content": "".join(reasoning),
            "usage": usage,
            "finish_reason": finish,
        }

    try:
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
                    if (
                        isinstance(error, dict)
                        and error.get("code") == "EMPTY_UPSTREAM_RESPONSE"
                    ):
                        raise EmptyResponseError(
                            "Router error: " + str(error), evidence()
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
                    telemetry.observe(chunk, time.monotonic())
                    usage = chunk.get("usage") or usage
                    if chunk.get("error"):
                        if (
                            isinstance(chunk["error"], dict)
                            and chunk["error"].get("code") == "context_length_exceeded"
                            and not (content or reasoning)
                        ):
                            raise ContextBudgetError(
                                chunk["error"].get("message", "Input context exhausted")
                            )
                        if (
                            isinstance(chunk["error"], dict)
                            and chunk["error"].get("code") == "EMPTY_UPSTREAM_RESPONSE"
                        ):
                            raise EmptyResponseError(
                                "Router error: " + str(chunk["error"]), evidence()
                            )
                        raise RuntimeError("Router error: " + str(chunk["error"]))
                    for choice in chunk.get("choices", []):
                        finish = choice.get("finish_reason") or finish
                        delta = choice.get("delta", {})
                        if delta.get("content"):
                            content.append(delta["content"])
                        if delta.get("reasoning_content"):
                            reasoning.append(delta["reasoning_content"])
                    if progress and time.monotonic() - last > 5:
                        progress(len("".join(content)) + len("".join(reasoning)))
                        last = time.monotonic()
        if done and finish == "stop" and not "".join(content).strip():
            raise EmptyResponseError(
                "Backend completed without a visible answer", evidence()
            )
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
    except BaseException as exc:
        if not getattr(exc, "evidence", None):
            exc.evidence = evidence()
        raise
    return evidence()
