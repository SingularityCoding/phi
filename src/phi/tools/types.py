"""定义可信 Tool 元数据，并从类型注解构造严格参数模型。"""

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
    """按 Tool 所需权限划分的粗粒度审批类别。"""

    READ_ONLY = "read_only"
    MUTATES_WORKSPACE = "mutates_workspace"
    UNCONFINED = "unconfined"


class _InjectedMarker:
    """标识 ``Annotated`` 元数据中由运行时注入的参数。"""

    pass


_INJECTED = _InjectedMarker()


class Injected:
    """把 handler 参数标记为由可信运行时 wiring 提供。"""

    def __class_getitem__(cls, item: Any) -> Any:
        """把 ``Injected[T]`` 展开为带私有标记的 ``Annotated[T]``。"""

        return Annotated[item, _INJECTED]


@dataclass(frozen=True)
class Tool:
    """一个可调用 Tool 的可信、不可变定义。"""

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
        """冻结 schema，并验证 OpenAI-compatible 名称和超时约束。"""

        # schema 是发送给 Model 的唯一来源，构造后不得被调用方悄悄修改。
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
    """递归冻结 JSON 容器，避免 Tool schema 在注册后漂移。"""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _is_injected(annotation: Any) -> bool:
    """判断类型注解是否含有 Phi 的可信注入标记。"""

    return get_origin(annotation) is Annotated and _INJECTED in get_args(annotation)[1:]


def _argument_model(handler: Callable[..., Any]) -> tuple[type[BaseModel], tuple[str, ...]]:
    """从 handler 签名生成严格 Pydantic 参数模型和注入参数列表。"""

    signature = inspect.signature(handler)
    hints = get_type_hints(handler, include_extras=True)
    if "return" not in hints:
        raise TypeError("tool handlers must have a return annotation")

    fields: dict[str, Any] = {}
    injected: list[str] = []
    for name, parameter in signature.parameters.items():
        # Tool 只能由关键字映射调用；可变参数无法生成明确且可验证的 wire schema。
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
            # 注入参数不会进入 Model 可见 schema，稍后由 dispatcher 从可信值填充。
            injected.append(name)
            continue
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[name] = (annotation, default)

    handler_name = getattr(handler, "__name__", handler.__class__.__name__)
    model_name = f"{handler_name.replace('_', ' ').title().replace(' ', '')}Arguments"
    # strict + forbid 防止 Pydantic 的隐式类型转换和未声明参数扩大执行面。
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
    """把带完整类型注解的本地 handler 装饰成 Tool 定义。"""

    def decorate(handler: Callable[..., Any]) -> Tool:
        """缓存参数模型和 schema，使执行时无需重新反射签名。"""

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
