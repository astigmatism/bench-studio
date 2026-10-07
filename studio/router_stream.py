"""Strict OpenAI-compatible SSE collection for trusted execution adapters.

Failures are classified by the router's `error.code` (LLM Router client
contract §9-§10); a benchmark never falls back to another model.
"""

import json
import os
import time
import httpx
from betterbench.telemetry import StreamTelemetry
from common import BenchmarkItemError, client_name as default_client, router_error, router_error_code


class ContextBudgetError(BenchmarkItemError):
    """The router rejected the input before generation because it cannot fit."""


class EmptyResponseError(RuntimeError):
    """Generation ended without an answer; partial evidence is not executable."""

    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence or {}


def _incomplete(chunk):
    router = chunk.get("x_router")
    return isinstance(router, dict) and router.get("status") == "incomplete"


async def completion(endpoint, payload, *, transport=None, progress=None, client_name=None):
    # `stream` is always explicit (contract §6).
    payload = dict(payload, stream=True, stream_options={"include_usage": True})
    headers = {
        "X-Client-Name": client_name
        or os.environ.get("BENCH_STUDIO_CLIENT_NAME")
        or default_client()
    }
    target = payload.get("model")
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
                "POST",
                endpoint.rstrip("/") + "/chat/completions",
                json=payload,
                headers=headers,
            ) as response:
                if response.is_error:
                    await response.aread()
                    try:
                        error = response.json().get("error", {})
                    except (ValueError, AttributeError):
                        error = {}
                    code = router_error_code(response.content)
                    if code == "context_length_exceeded":
                        raise ContextBudgetError(
                            error.get("message", "Input context exhausted")
                            if isinstance(error, dict)
                            else "Input context exhausted"
                        )
                    if code == "EMPTY_UPSTREAM_RESPONSE":
                        raise EmptyResponseError(
                            "Router error: " + str(error), evidence()
                        )
                    raise router_error(
                        response.status_code, response.content, target=target
                    )
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
                    if chunk.get("error") or _incomplete(chunk):
                        # An error inside the stream ends it as incomplete (§9).
                        code = router_error_code(chunk)
                        error = chunk.get("error")
                        if code == "context_length_exceeded" and not (content or reasoning):
                            raise ContextBudgetError(
                                error.get("message", "Input context exhausted")
                                if isinstance(error, dict)
                                else "Input context exhausted"
                            )
                        if code == "EMPTY_UPSTREAM_RESPONSE":
                            raise EmptyResponseError(
                                "Router error: " + str(error or code), evidence()
                            )
                        raise router_error(None, chunk, target=target, stream=True)
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
