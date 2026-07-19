from __future__ import annotations

from collections.abc import Iterable

from phi.model import ModelInfo


class ModelResolutionError(ValueError):
    """The requested effective Model cannot be selected from the available catalog."""


def optional_model_id(value: str | None) -> str | None:
    """Normalize an optional Model ID without choosing a fallback."""

    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def require_explicit_model_id(value: str, *, option: str = "--model") -> str:
    """Normalize a user-supplied Model ID or report the option contract."""

    model_id = optional_model_id(value)
    if model_id is None:
        raise ModelResolutionError(f"{option} must contain non-whitespace text")
    return model_id


def require_available_model(
    model_id: str,
    available_models: Iterable[ModelInfo],
) -> ModelInfo:
    """Resolve one normalized ID against a discovered Model catalog."""

    model_info = next((model for model in available_models if model.id == model_id), None)
    if model_info is None:
        raise ModelResolutionError(f"Model {model_id!r} is not available")
    return model_info


def resolve_available_model(
    *candidates: str | None,
    available_models: Iterable[ModelInfo],
    missing_message: str,
) -> tuple[str, ModelInfo]:
    """Choose the first non-blank candidate and validate it against the catalog."""

    model_id = next(
        (normalized for candidate in candidates if (normalized := optional_model_id(candidate))),
        None,
    )
    if model_id is None:
        raise ModelResolutionError(missing_message)
    return model_id, require_available_model(model_id, available_models)
