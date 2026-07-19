from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from phi.tools.types import Tool


class ToolRegistry:
    """Unique-name catalog and Model-visible schema source for Tools."""

    def __init__(self, tools: list[Tool] | tuple[Tool, ...] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for registered_tool in tools:
            self.register(registered_tool)

    def register(self, registered_tool: Tool) -> None:
        if registered_tool.name in self._tools:
            raise ValueError(f"tool {registered_tool.name!r} is already registered")
        self._tools[registered_tool.name] = registered_tool

    def register_many(self, registered_tools: list[Tool] | tuple[Tool, ...]) -> None:
        """Register a batch atomically after validating every Tool name."""

        additions: dict[str, Tool] = {}
        for registered_tool in registered_tools:
            if registered_tool.name in self._tools or registered_tool.name in additions:
                raise ValueError(f"tool {registered_tool.name!r} is already registered")
            additions[registered_tool.name] = registered_tool
        self._tools.update(additions)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def select(self, names: tuple[str, ...] | None = None) -> ToolRegistry:
        """Snapshot all Tools or an ordered explicit subset into a new registry."""

        selected_names = tuple(self._tools) if names is None else names
        selected: list[Tool] = []
        for name in selected_names:
            registered_tool = self._tools.get(name)
            if registered_tool is None:
                raise KeyError(name)
            selected.append(registered_tool)
        return ToolRegistry(selected)

    def specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": registered_tool.name,
                    "description": registered_tool.description,
                    "parameters": _mutable_json(registered_tool.args_schema),
                },
            }
            for registered_tool in sorted(self._tools.values(), key=lambda item: item.name)
        ]


def build_default_registry() -> ToolRegistry:
    """Construct the deterministic catalog of Phi's built-in Tools."""

    from phi.tools.builtin import BUILTIN_TOOLS

    return ToolRegistry(BUILTIN_TOOLS)


def _mutable_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(item) for item in value]
    return value
