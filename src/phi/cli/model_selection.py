"""CLI 与 TUI 共用的 Model 标识规范化和可用性解析。"""

from __future__ import annotations

from collections.abc import Iterable

from phi.model import ModelInfo


class ModelResolutionError(ValueError):
    """表示无法从可用目录选择请求的有效 Model。"""


def optional_model_id(value: str | None) -> str | None:
    """规范化可选 Model ID，但不自行选择后备值。"""

    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def require_explicit_model_id(value: str, *, option: str = "--model") -> str:
    """规范化用户提供的 Model ID，空值则报告选项契约。"""

    model_id = optional_model_id(value)
    if model_id is None:
        raise ModelResolutionError(f"{option} must contain non-whitespace text")
    return model_id


def require_available_model(
    model_id: str,
    available_models: Iterable[ModelInfo],
) -> ModelInfo:
    """在已发现的 Model 目录中解析一个规范化 ID。"""

    model_info = next((model for model in available_models if model.id == model_id), None)
    if model_info is None:
        raise ModelResolutionError(f"Model {model_id!r} is not available")
    return model_info


def resolve_available_model(
    *candidates: str | None,
    available_models: Iterable[ModelInfo],
    missing_message: str,
) -> tuple[str, ModelInfo]:
    """选择首个非空候选，并用可用 Model 目录验证。"""

    # 调用方按优先级传入候选，因此这里不能重排或偷偷采用其他身份。
    model_id = next(
        (normalized for candidate in candidates if (normalized := optional_model_id(candidate))),
        None,
    )
    if model_id is None:
        raise ModelResolutionError(missing_message)
    return model_id, require_available_model(model_id, available_models)
