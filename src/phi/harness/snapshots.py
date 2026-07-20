"""为 Run 与 Event 构造递归不可变、与活跃状态隔离的观察快照。"""

from __future__ import annotations

from enum import Enum
from typing import Any, Never, cast

from phi.model import (
    FinishEvent,
    ModelEvent,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolCallDelta,
    ToolResult,
    UsageEvent,
)


class _FrozenList(list[Any]):
    """保留 list 接口但拒绝所有原地修改的递归快照容器。"""

    def _immutable(self, *args: object, **kwargs: object) -> Never:
        """为所有 list 修改入口抛出一致的不可变错误。"""

        del args, kwargs
        raise TypeError("Event and Run snapshots are immutable")

    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    __setitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable

    def __copy__(self) -> _FrozenList:
        """浅拷贝时复用已经不可变的当前对象。"""

        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _FrozenList:
        """深拷贝时复用已经递归冻结的当前对象。"""

        del memo
        return self


class _FrozenDict(dict[str, Any]):
    """保留 dict 接口但拒绝所有原地修改的递归快照容器。"""

    def _immutable(self, *args: object, **kwargs: object) -> Never:
        """为所有 dict 修改入口抛出一致的不可变错误。"""

        del args, kwargs
        raise TypeError("Event and Run snapshots are immutable")

    __delitem__ = _immutable
    __ior__ = _immutable
    __setitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __copy__(self) -> _FrozenDict:
        """浅拷贝时复用已经不可变的当前对象。"""

        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _FrozenDict:
        """深拷贝时复用已经递归冻结的当前对象。"""

        del memo
        return self


class _OpaqueFrozenError(Exception):
    """在原异常无法安全重建时仅保留其类型名的不可变替代值。"""

    def __init__(self, error_type_name: str) -> None:
        """记录原异常类型名，并立即锁定快照。"""

        super().__init__(error_type_name)
        super().__setattr__("_snapshot_locked", True)

    def __setattr__(self, name: str, value: object) -> None:
        """把属性写入统一交给快照锁检查。"""

        _set_error_snapshot_attribute(self, name, value)

    def with_traceback(self, traceback: object) -> Never:
        """拒绝通过 with_traceback 改写异常快照。"""

        del traceback
        return _reject_error_snapshot_mutation()


_FROZEN_ERROR_TYPES: dict[type[Exception], type[Exception]] = {}


def freeze_request(request: ModelRequest) -> ModelRequest:
    """重建 Model Request，并递归冻结消息与工具 wire 值。"""

    return ModelRequest(
        messages=cast(list[dict[str, Any]], _freeze_json(request.messages)),
        tools=cast(list[dict[str, Any]], _freeze_json(request.tools)),
        model=request.model,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
    )


def freeze_response(response: ModelResponse) -> ModelResponse:
    """重建 Model Response，并冻结 Tool Call 列表与原始载荷。"""

    return ModelResponse(
        content=response.content,
        reasoning=response.reasoning,
        tool_calls=cast(
            list[ToolCall],
            _FrozenList([freeze_tool_call(call) for call in response.tool_calls]),
        ),
        usage=response.usage,
        finish_reason=response.finish_reason,
        raw=cast(dict[str, Any], _freeze_json(response.raw)),
    )


def freeze_tool_call(call: ToolCall) -> ToolCall:
    """重建 Tool Call，并递归冻结其参数对象。"""

    return ToolCall(
        id=call.id,
        name=call.name,
        arguments=cast(dict[str, Any], _freeze_json(call.arguments)),
    )


def freeze_tool_result(result: ToolResult) -> ToolResult:
    """复制只含标量字段的 Tool Result。"""

    return ToolResult(call_id=result.call_id, output=result.output, error=result.error)


def freeze_model_event(event: ModelEvent) -> ModelEvent:
    """冻结 Model Event 中可能携带的 raw 或参数分片数据。"""

    # 只有三类 Event 需要重建；纯文本 delta 与冻结 Usage 本身已不可变。
    if isinstance(event, FinishEvent):
        return FinishEvent(event.finish_reason, cast(dict[str, Any], _freeze_json(event.raw)))
    if isinstance(event, UsageEvent):
        return UsageEvent(event.usage, cast(dict[str, Any], _freeze_json(event.raw)))
    if isinstance(event, ToolCallDelta):
        return ToolCallDelta(
            index=event.index,
            id=event.id,
            name=event.name,
            arguments_fragment=event.arguments_fragment,
        )
    return event


def freeze_error(error: Exception | None) -> Exception | None:
    """递归复制异常图，并使副本在投递后不可修改。"""

    if error is None:
        return None
    return _freeze_error(error, {})


def _freeze_error(error: Exception, memo: dict[int, Any]) -> Exception:
    """在 memo 协助下保留异常类型、状态、因果链与循环引用。"""

    # 异常的 cause/context 或自定义属性可能形成环，必须先查询对象身份缓存。
    existing = memo.get(id(error))
    if isinstance(existing, Exception):
        return existing
    try:
        # 动态冻结子类保留 isinstance 语义，同时覆盖所有属性修改入口。
        frozen_type = _frozen_error_type(type(error))
        constructor_arguments, state = _error_reduction(error)
        frozen_arguments = tuple(
            _freeze_error_value(argument, memo) for argument in constructor_arguments
        )
        snapshot = frozen_type.__new__(frozen_type, *frozen_arguments)
    except Exception:
        # 不可信自定义异常可能拒绝 reduce/new；此时安全降级而不是泄漏原对象。
        opaque = _OpaqueFrozenError(type(error).__name__)
        memo[id(error)] = opaque
        return opaque

    # 在递归属性前登记占位，确保异常自引用能指回同一快照。
    memo[id(error)] = snapshot
    attributes = dict(state)
    attributes.update(vars(error))
    for name, value in attributes.items():
        if not isinstance(name, str):
            continue
        BaseException.__setattr__(snapshot, name, _freeze_error_value(value, memo))
    # 显式复制异常链控制字段；它们不一定出现在 vars(error) 中。
    if error.__cause__ is not None:
        BaseException.__setattr__(
            snapshot,
            "__cause__",
            _freeze_related_error(error.__cause__, memo),
        )
    if error.__context__ is not None:
        BaseException.__setattr__(
            snapshot,
            "__context__",
            _freeze_related_error(error.__context__, memo),
        )
    BaseException.__setattr__(snapshot, "__suppress_context__", error.__suppress_context__)
    BaseException.__setattr__(snapshot, "__traceback__", error.__traceback__)
    # 所有状态复制完成后再锁定，构造阶段仍可使用 BaseException 底层写入口。
    BaseException.__setattr__(snapshot, "_snapshot_locked", True)
    return snapshot


def _freeze_related_error(
    error: BaseException,
    memo: dict[int, Any],
) -> Exception:
    """冻结 cause/context；非 Exception 的 BaseException 降级为不透明值。"""

    if isinstance(error, Exception):
        return _freeze_error(error, memo)
    return _OpaqueFrozenError(type(error).__name__)


def _freeze_json(value: Any) -> Any:
    """递归冻结 JSON 风格容器，同时保留标量值。"""

    if isinstance(value, (_FrozenList, _FrozenDict)):
        return value
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze_json(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _frozen_error_type(error_type: type[Exception]) -> type[Exception]:
    """取得或创建保留原异常继承关系的动态冻结子类。"""

    existing = _FROZEN_ERROR_TYPES.get(error_type)
    if existing is not None:
        return existing
    # 缓存动态类型，避免每个 Event 快照都制造新的 class 对象。
    frozen_type = cast(
        type[Exception],
        type(
            f"Frozen{error_type.__name__}",
            (error_type,),
            {
                "__module__": __name__,
                "__setattr__": _set_error_snapshot_attribute,
                "with_traceback": _immutable_error_with_traceback,
            },
        ),
    )
    _FROZEN_ERROR_TYPES[error_type] = frozen_type
    return frozen_type


def _set_error_snapshot_attribute(
    error: BaseException,
    name: str,
    value: object,
) -> None:
    """仅允许冻结异常在锁定前写入属性。"""

    if getattr(error, "_snapshot_locked", False):
        raise TypeError("Event error snapshots are immutable")
    BaseException.__setattr__(error, name, value)


def _immutable_error_with_traceback(
    error: BaseException,
    traceback: object,
) -> Never:
    """作为动态冻结异常的 with_traceback 拒绝实现。"""

    del error, traceback
    return _reject_error_snapshot_mutation()


def _reject_error_snapshot_mutation() -> Never:
    """抛出统一的异常快照不可变错误。"""

    raise TypeError("Event error snapshots are immutable")


def _error_reduction(error: Exception) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """从异常 reduction 中尽力提取构造参数与附加状态。"""

    # 自定义异常的 __reduce__ 也可能失败或返回非标准形状，因此逐级回退到 args。
    try:
        reduced = error.__reduce__()
    except Exception:
        return error.args, {}
    if not isinstance(reduced, tuple) or len(reduced) < 2:
        return error.args, {}
    arguments = reduced[1]
    if not isinstance(arguments, tuple):
        arguments = error.args
    state = reduced[2] if len(reduced) > 2 else {}
    if not isinstance(state, dict):
        state = {}
    return arguments, state


def _freeze_error_value(value: Any, memo: dict[int, Any]) -> Any:
    """冻结异常属性值，并安全处理任意容器、循环与未知对象。"""

    if value is None or isinstance(value, (str, bytes, int, float, complex, bool, Enum)):
        return value
    if isinstance(value, Exception):
        return _freeze_error(value, memo)
    if isinstance(value, dict):
        # 容器在递归前先写入 memo，才能正确保留自引用结构。
        existing = memo.get(id(value))
        if existing is not None:
            return existing
        frozen_dict = _FrozenDict()
        memo[id(value)] = frozen_dict
        for key, item in value.items():
            # Event 快照只需可诊断性；复杂键降级为类型名，避免携带可变对象。
            frozen_key = (
                key if isinstance(key, (str, int, float, bool, bytes)) else type(key).__name__
            )
            dict.__setitem__(frozen_dict, frozen_key, _freeze_error_value(item, memo))
        return frozen_dict
    if isinstance(value, list):
        existing = memo.get(id(value))
        if existing is not None:
            return existing
        frozen_list = _FrozenList()
        memo[id(value)] = frozen_list
        for item in value:
            list.append(frozen_list, _freeze_error_value(item, memo))
        return frozen_list
    if isinstance(value, tuple):
        # 不可变容器无法先创建空壳，用临时标记阻断极端的循环引用。
        existing = memo.get(id(value))
        if existing is not None:
            return existing
        memo[id(value)] = type(value).__name__
        frozen_tuple = tuple(_freeze_error_value(item, memo) for item in value)
        memo[id(value)] = frozen_tuple
        return frozen_tuple
    if isinstance(value, (set, frozenset)):
        existing = memo.get(id(value))
        if existing is not None:
            return existing
        memo[id(value)] = type(value).__name__
        frozen_set = frozenset(_freeze_error_value(item, memo) for item in value)
        memo[id(value)] = frozen_set
        return frozen_set
    # 未知对象可能暴露可变外部状态；只保留类型名以维持观察边界。
    return type(value).__name__
