"""No-inference integration test against the installed pinned Harbor package."""

import asyncio
from pathlib import Path
from studio import harbor_agent
from harbor.llms.base import OutputLengthExceededError


async def run():
    agent = harbor_agent.RouterTerminus2(
        logs_dir=Path("/tmp/transport-test"),
        model_name="openai/local-test",
        max_turns=4,
        record_terminal_session=False,
        api_base="http://router/v1",
        temperature=0,
        reasoning_effort="none",
        model_info={"max_input_tokens": 8192, "max_output_tokens": 2048},
        llm_kwargs={"seed": 42, "top_p": 1, "max_tokens": 2048},
    )

    async def fake(endpoint, payload, **kwargs):
        assert payload["max_tokens"] == 2048 and payload["reasoning_effort"] == "none"
        assert "previous_response_id" not in payload and len(payload["messages"]) == 2
        return {
            "content": "{}",
            "reasoning_content": "",
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 25, "completion_tokens": 4},
        }

    harbor_agent.completion = fake
    r = await agent._llm.call(
        "task",
        message_history=[{"role": "system", "content": "test"}],
        previous_response_id=None,
    )
    assert r.content == "{}" and r.usage.completion_tokens == 4
    assert not harbor_agent.RouterLLM.infrastructure_errors

    async def broken(*a, **k):
        raise RuntimeError("Missing usage")

    harbor_agent.completion = broken
    try:
        await agent._llm.call("test", previous_response_id=None)
    except RuntimeError:
        pass
    assert harbor_agent.RouterLLM.infrastructure_errors == {"local-test": ["Missing usage"]}
    print("Pinned Terminus 2 transport integration PASS; no model calls")


asyncio.run(run())
