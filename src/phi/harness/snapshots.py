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
    def _immutable(self, *args: object, **kwargs: object) -> Never:
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
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _FrozenList:
        del memo
        return self


class _FrozenDict(dict[str, Any]):
    def _immutable(self, *args: object, **kwargs: object) -> Never:
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
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _FrozenDict:
        del memo
        return self


class _OpaqueFrozenError(Exception):
    def __init__(self, error_type_name: str) -> None:
        super().__init__(error_type_name)
        super().__setattr__("_snapshot_locked", True)

    def __setattr__(self, name: str, value: object) -> None:
        _set_error_snapshot_attribute(self, name, value)

    def with_traceback(self, traceback: object) -> Never:
        del traceback
        return _reject_error_snapshot_mutation()


_FROZEN_ERROR_TYPES: dict[type[Exception], type[Exception]] = {}


def freeze_request(request: ModelRequest) -> ModelRequest:
    return ModelRequest(
        messages=cast(list[dict[str, Any]], _freeze_json(request.messages)),
        tools=cast(list[dict[str, Any]], _freeze_json(request.tools)),
        model=request.model,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
    )


def freeze_response(response: ModelResponse) -> ModelResponse:
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
    return ToolCall(
        id=call.id,
        name=call.name,
        arguments=cast(dict[str, Any], _freeze_json(call.arguments)),
    )


def freeze_tool_result(result: ToolResult) -> ToolResult:
    return ToolResult(call_id=result.call_id, output=result.output, error=result.error)


def freeze_model_event(event: ModelEvent) -> ModelEvent:
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
    if error is None:
        return None
    return _freeze_error(error, {})


def _freeze_error(error: Exception, memo: dict[int, Any]) -> Exception:
    existing = memo.get(id(error))
    if isinstance(existing, Exception):
        return existing
    try:
        frozen_type = _frozen_error_type(type(error))
        constructor_arguments, state = _error_reduction(error)
        frozen_arguments = tuple(
            _freeze_error_value(argument, memo) for argument in constructor_arguments
        )
        snapshot = frozen_type.__new__(frozen_type, *frozen_arguments)
    except Exception:
        opaque = _OpaqueFrozenError(type(error).__name__)
        memo[id(error)] = opaque
        return opaque

    memo[id(error)] = snapshot
    attributes = dict(state)
    attributes.update(vars(error))
    for name, value in attributes.items():
        if not isinstance(name, str):
            continue
        BaseException.__setattr__(snapshot, name, _freeze_error_value(value, memo))
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
    BaseException.__setattr__(snapshot, "_snapshot_locked", True)
    return snapshot


def _freeze_related_error(
    error: BaseException,
    memo: dict[int, Any],
) -> Exception:
    if isinstance(error, Exception):
        return _freeze_error(error, memo)
    return _OpaqueFrozenError(type(error).__name__)


def _freeze_json(value: Any) -> Any:
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
    existing = _FROZEN_ERROR_TYPES.get(error_type)
    if existing is not None:
        return existing
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
    if getattr(error, "_snapshot_locked", False):
        raise TypeError("Event error snapshots are immutable")
    BaseException.__setattr__(error, name, value)


def _immutable_error_with_traceback(
    error: BaseException,
    traceback: object,
) -> Never:
    del error, traceback
    return _reject_error_snapshot_mutation()


def _reject_error_snapshot_mutation() -> Never:
    raise TypeError("Event error snapshots are immutable")


def _error_reduction(error: Exception) -> tuple[tuple[Any, ...], dict[str, Any]]:
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
    if value is None or isinstance(value, (str, bytes, int, float, complex, bool, Enum)):
        return value
    if isinstance(value, Exception):
        return _freeze_error(value, memo)
    if isinstance(value, dict):
        existing = memo.get(id(value))
        if existing is not None:
            return existing
        frozen_dict = _FrozenDict()
        memo[id(value)] = frozen_dict
        for key, item in value.items():
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
    return type(value).__name__
