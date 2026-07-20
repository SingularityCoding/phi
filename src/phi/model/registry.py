"""从 Proxy 发现可用 Model，并校验可信的容量元数据。"""

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
    """列出 Proxy 暴露的 Model，并仅保留可信元数据。

    Args:
        config: Model 端点、凭据和超时配置。
        client: 可选的外部 HTTP 客户端；传入时其生命周期仍由调用方管理。

    Returns:
        按 Proxy 响应顺序排列的 Model 元数据。

    Raises:
        ModelTimeoutError: 请求超过配置的超时时间。
        ModelHTTPError: 传输失败或 Proxy 返回非成功状态。
        ModelProtocolError: 成功响应不符合注册表协议。
    """

    http_client = client if client is not None else httpx.AsyncClient()
    owns_client = client is None
    # 只有本函数创建的客户端才在退出时关闭，避免破坏调用方共享连接池。
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
    """校验注册表响应，并转换为内部 ModelInfo 列表。"""

    raw = _object(raw_value, "Model registry response")
    data = raw.get("data")
    if not isinstance(data, list):
        raise ModelProtocolError("Model registry data must be a list")

    # 每一项都独立校验，防止不可信元数据进入 Context 预算策略。
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
    """把不可信值收窄为 JSON 对象，否则抛出协议错误。"""

    if not isinstance(value, dict):
        raise ModelProtocolError(f"{field} must be an object")
    return cast(dict[str, Any], value)


def _optional_token_limit(value: object, field: str) -> int | None:
    """校验可缺省的非负 token 容量字段。"""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelProtocolError(f"{field} must be a non-negative integer or null")
    return value
