from __future__ import annotations

from typing import Any, cast

import httpx

from phi.model.errors import ModelHTTPError, ModelProtocolError, ModelTimeoutError
from phi.model.types import ModelConfig, ModelInfo


async def list_available_models(
    config: ModelConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[ModelInfo]:
    """List Models exposed by the Proxy, retaining only trustworthy metadata."""

    http_client = client if client is not None else httpx.AsyncClient()
    owns_client = client is None
    try:
        try:
            response = await http_client.get(
                f"{config.base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {config.api_key.get_secret_value()}"},
                timeout=config.request_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError from exc
        except httpx.RequestError as exc:
            raise ModelHTTPError(status_code=0, body=str(exc)) from exc

        if not response.is_success:
            raise ModelHTTPError(status_code=response.status_code, body=response.text)
        try:
            raw: object = response.json()
        except ValueError as exc:
            raise ModelProtocolError("Model registry response body is not valid JSON") from exc
        return _parse_model_list(raw)
    finally:
        if owns_client:
            await http_client.aclose()


def _parse_model_list(raw_value: object) -> list[ModelInfo]:
    raw = _object(raw_value, "Model registry response")
    data = raw.get("data")
    if not isinstance(data, list):
        raise ModelProtocolError("Model registry data must be a list")

    models: list[ModelInfo] = []
    for index, raw_model in enumerate(data):
        model = _object(raw_model, f"Model registry entry at index {index}")
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ModelProtocolError(
                f"Model registry entry at index {index} id must be a non-empty string"
            )
        models.append(
            ModelInfo(
                id=model_id,
                max_input_tokens=_optional_token_limit(
                    model.get("max_input_tokens"),
                    f"Model registry entry {model_id} max_input_tokens",
                ),
                max_output_tokens=_optional_token_limit(
                    model.get("max_output_tokens"),
                    f"Model registry entry {model_id} max_output_tokens",
                ),
            )
        )
    return models


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelProtocolError(f"{field} must be an object")
    return cast(dict[str, Any], value)


def _optional_token_limit(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelProtocolError(f"{field} must be a non-negative integer or null")
    return value
