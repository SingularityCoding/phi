import httpx
from pydantic import SecretStr

from phi.model import ModelConfig, ModelInfo, list_available_models


async def test_list_available_models_returns_only_trustworthy_metadata():
    config = ModelConfig(
        base_url="https://proxy.example/v1/",
        api_key=SecretStr("test-key"),
        default_model="course-model",
        request_timeout_seconds=30.0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == "https://proxy.example/v1/models"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "reasoning-model",
                        "created": 1,
                        "owned_by": "placeholder",
                        "max_input_tokens": 64_000,
                        "max_output_tokens": 8_000,
                    },
                    {
                        "id": "plain-model",
                        "created": 1,
                        "owned_by": "placeholder",
                    },
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        models = await list_available_models(config, client=client)

    assert models == [
        ModelInfo(
            id="reasoning-model",
            max_input_tokens=64_000,
            max_output_tokens=8_000,
        ),
        ModelInfo(id="plain-model"),
    ]
