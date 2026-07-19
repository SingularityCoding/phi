from __future__ import annotations

import inspect
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ConfigDict, create_model


class ApprovalClass(StrEnum):
    """Coarse policy bucket for a Tool's authority requirements."""

    READ_ONLY = "read_only"
    MUTATES_WORKSPACE = "mutates_workspace"
    UNCONFINED = "unconfined"


class _InjectedMarker:
    pass


_INJECTED = _InjectedMarker()


class Injected:
    """Mark a handler parameter as supplied by trusted runtime wiring."""

    def __class_getitem__(cls, item: Any) -> Any:
        return Annotated[item, _INJECTED]


@dataclass(frozen=True)
class Tool:
    """Trusted, immutable definition of one callable Tool."""

    name: str
    description: str
    handler: Callable[..., Any]
    args_schema: Mapping[str, Any]
    args_model: type[BaseModel] | None = None
    approval_class: ApprovalClass = ApprovalClass.READ_ONLY
    timeout_seconds: float | None = None
    timeout_parameter: str | None = None
    injected_parameters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "args_schema", _freeze_json(self.args_schema))
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.name):
            raise ValueError("tool name must contain only letters, digits, underscores, or hyphens")
        if len(self.name) > 64:
            raise ValueError("tool name must not exceed 64 characters")
        if not self.description.strip():
            raise ValueError("tool description must not be empty")
        if self.timeout_seconds is not None and (
            not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
        ):
            raise ValueError("tool timeout must be finite and positive")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _is_injected(annotation: Any) -> bool:
    return get_origin(annotation) is Annotated and _INJECTED in get_args(annotation)[1:]


def _argument_model(handler: Callable[..., Any]) -> tuple[type[BaseModel], tuple[str, ...]]:
    signature = inspect.signature(handler)
    hints = get_type_hints(handler, include_extras=True)
    if "return" not in hints:
        raise TypeError("tool handlers must have a return annotation")

    fields: dict[str, Any] = {}
    injected: list[str] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise TypeError(
                "tool handlers cannot use positional-only, *args, or **kwargs parameters"
            )
        if name not in hints:
            raise TypeError(f"tool handler parameter {name!r} must have an annotation")
        annotation = hints[name]
        if _is_injected(annotation):
            injected.append(name)
            continue
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[name] = (annotation, default)

    handler_name = getattr(handler, "__name__", handler.__class__.__name__)
    model_name = f"{handler_name.replace('_', ' ').title().replace(' ', '')}Arguments"
    model = create_model(
        model_name,
        __config__=ConfigDict(extra="forbid", strict=True),
        **fields,
    )
    return model, tuple(injected)


def tool(
    *,
    name: str,
    description: str,
    approval_class: ApprovalClass = ApprovalClass.READ_ONLY,
    timeout_seconds: float | None = None,
    timeout_parameter: str | None = None,
) -> Callable[[Callable[..., Any]], Tool]:
    """Build a local Tool definition from a fully annotated handler."""

    def decorate(handler: Callable[..., Any]) -> Tool:
        args_model, injected_parameters = _argument_model(handler)
        return Tool(
            name=name,
            description=description,
            handler=handler,
            args_schema=args_model.model_json_schema(),
            args_model=args_model,
            approval_class=approval_class,
            timeout_seconds=timeout_seconds,
            timeout_parameter=timeout_parameter,
            injected_parameters=injected_parameters,
        )

    return decorate
