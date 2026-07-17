import os

import pytest

from phi.model import (
    FinishEvent,
    ModelConfig,
    ModelRequest,
    OpenAICompatibleModel,
    ResponseAssembler,
)
from phi.settings import Settings

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(
        os.getenv("PHI_RUN_MODEL_CONTRACTS") != "1",
        reason="set PHI_RUN_MODEL_CONTRACTS=1 to run live Model contracts",
    ),
]


@pytest.fixture
def live_config() -> ModelConfig:
    settings = Settings()
    if not settings.api_key.get_secret_value() or not settings.default_model:
        pytest.fail("live Model contracts require PHI_API_KEY and PHI_DEFAULT_MODEL")
    return ModelConfig(
        base_url=settings.base_url,
        api_key=settings.api_key,
        default_model=settings.default_model,
        request_timeout_seconds=settings.request_timeout_seconds,
    )


async def test_proxy_basic_contract(live_config: ModelConfig):
    async with OpenAICompatibleModel(live_config) as model:
        response = await model.request(
            ModelRequest(
                messages=[{"role": "user", "content": "Reply with only the word OK."}],
                max_tokens=64,
            )
        )

    assert response.content is not None
    assert response.usage is not None
    assert response.raw


async def test_proxy_streaming_contract(live_config: ModelConfig):
    assembler = ResponseAssembler()
    async with OpenAICompatibleModel(live_config) as model:
        events = [
            event
            async for event in model.request_stream(
                ModelRequest(
                    messages=[{"role": "user", "content": "Reply briefly with hello."}],
                    max_tokens=64,
                )
            )
        ]

    for event in events:
        assembler.absorb(event)
    response = assembler.build()
    assert any(isinstance(event, FinishEvent) for event in events)
    assert response.content is not None or response.reasoning is not None
    assert response.usage is not None


async def test_proxy_constrained_budget_contract(live_config: ModelConfig):
    async with OpenAICompatibleModel(live_config) as model:
        response = await model.request(
            ModelRequest(
                messages=[{"role": "user", "content": "What is 2 + 2?"}],
                max_tokens=1,
            )
        )

    assert response.finish_reason == "length"
    assert response.usage is not None


async def test_proxy_tool_call_contract(live_config: ModelConfig):
    weather_tool = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Look up the weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    }
    async with OpenAICompatibleModel(live_config) as model:
        response = await model.request(
            ModelRequest(
                messages=[
                    {
                        "role": "user",
                        "content": "Call get_weather for Shanghai; do not answer directly.",
                    }
                ],
                tools=[weather_tool],
                max_tokens=128,
            )
        )

    assert response.tool_calls
    assert response.tool_calls[0].name == "get_weather"
    assert response.tool_calls[0].arguments.get("city")
