"""Pinned Terminus 2 with an explicit streaming transport adapter.

The agent's prompts, parser, commands and turn accounting remain upstream.
Only the model transport is replaced; production router configuration is untouched.
"""

import json
import os
import asyncio
from pathlib import Path
from harbor.agents.terminus_2.terminus_2 import Terminus2
from harbor.llms.base import (
    BaseLLM,
    LLMResponse,
    OutputLengthExceededError,
    ContextLengthExceededError,
)
from harbor.llms.lite_llm import STRUCTURED_RESPONSE_PROMPT_TEMPLATE
from harbor.models.metric import UsageInfo
from .router_stream import completion, ContextBudgetError


class RouterLLM(BaseLLM):
    infrastructure_errors = {}

    async def call(self, *args, **kwargs):
        try:
            return await self._call(*args, **kwargs)
        except ContextBudgetError as exc:
            raise ContextLengthExceededError(str(exc)) from exc
        except (OutputLengthExceededError, ContextLengthExceededError):
            raise
        except Exception as exc:
            self.infrastructure_errors.setdefault(self.model, []).append(str(exc))
            raise

    def __init__(self, model_name, api_base, model_info, parameters):
        self.model = model_name.removeprefix("openai/")
        self.endpoint = api_base
        self.info = model_info
        self.parameters = parameters

    def get_model_context_limit(self):
        return self.info["max_input_tokens"]

    def get_model_output_limit(self):
        return self.info["max_output_tokens"]

    async def _call(
        self,
        prompt,
        message_history=None,
        response_format=None,
        logging_path=None,
        **kwargs,
    ):
        if kwargs.pop("previous_response_id", None) is not None:
            raise ValueError("Stateful Responses API is not used by this adapter")
        if response_format:
            schema = (
                response_format
                if isinstance(response_format, dict)
                else response_format.model_json_schema()
            )
            prompt = STRUCTURED_RESPONSE_PROMPT_TEMPLATE.format(
                schema=json.dumps(schema), prompt=prompt
            )
        messages = [
            m.model_dump(exclude_none=True) if hasattr(m, "model_dump") else m
            for m in (message_history or [])
        ]
        messages.append({"role": "user", "content": prompt})
        permitted = {
            "temperature",
            "top_p",
            "seed",
            "max_tokens",
            "reasoning_effort",
            "reasoning_budget_tokens",
        }
        if set(kwargs) - permitted:
            raise ValueError(
                "Unsupported Harbor call settings: "
                + str(sorted(set(kwargs) - permitted))
            )
        payload = {
            "model": self.model,
            "messages": messages,
            **self.parameters,
            **kwargs,
        }
        if os.environ.get("BENCH_STUDIO_MANIFEST"):
            from common import read_json, wait_for_runtime

            manifest = read_json(os.environ["BENCH_STUDIO_MANIFEST"])
            target = next(
                t
                for t, value in manifest["resolved"].items()
                if value["canonical"] == self.model
            )
            await asyncio.to_thread(
                wait_for_runtime,
                manifest["settings"],
                target,
                manifest["resolved"][target],
            )
        print(self.model, "request started; input messages", len(messages), flush=True)
        result = await completion(
            self.endpoint,
            payload,
            progress=lambda n: print(
                self.model, "generating:", n, "characters received", flush=True
            ),
        )
        if logging_path:
            Path(logging_path).parent.mkdir(parents=True, exist_ok=True)
            Path(logging_path).write_text(
                json.dumps({"request": payload, "response": result}, indent=2)
            )
        usage = result["usage"]
        print(
            self.model,
            "request finished:",
            usage["completion_tokens"],
            "tokens;",
            result["finish_reason"],
            flush=True,
        )
        if result["finish_reason"] == "length":
            raise OutputLengthExceededError(
                "Configured output budget exhausted",
                truncated_response=result["content"],
            )
        return LLMResponse(
            content=result["content"],
            reasoning_content=result["reasoning_content"] or None,
            model_name=self.model,
            usage=UsageInfo(
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                cache_tokens=0,
                cost_usd=0,
            ),
        )


class RouterTerminus2(Terminus2):
    def _init_llm(
        self,
        llm_backend,
        model_name,
        temperature,
        collect_rollout_details,
        llm_kwargs,
        api_base,
        session_id,
        max_thinking_tokens,
        reasoning_effort,
        model_info,
        use_responses_api,
    ):
        parameters = dict(llm_kwargs or {})
        if temperature is not None:
            parameters["temperature"] = temperature
        if reasoning_effort and reasoning_effort != "default":
            parameters["reasoning_effort"] = reasoning_effort
        return RouterLLM(model_name, api_base, model_info, parameters)
