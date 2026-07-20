"""维护唯一命名的 Tool 目录，并生成发送给 Model 的 schema。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from phi.tools.types import Tool


class ToolRegistry:
    """以唯一名称保存 Tool，并作为 Model 可见 schema 的来源。"""

    def __init__(self, tools: list[Tool] | tuple[Tool, ...] = ()) -> None:
        """按给定顺序注册初始 Tool，复用单项重复检查。"""

        self._tools: dict[str, Tool] = {}
        for registered_tool in tools:
            self.register(registered_tool)

    def register(self, registered_tool: Tool) -> None:
        """注册一个 Tool；名称重复时拒绝覆盖已有定义。"""

        if registered_tool.name in self._tools:
            raise ValueError(f"tool {registered_tool.name!r} is already registered")
        self._tools[registered_tool.name] = registered_tool

    def register_many(self, registered_tools: list[Tool] | tuple[Tool, ...]) -> None:
        """先验证整批名称，再原子式地注册全部 Tool。"""

        additions: dict[str, Tool] = {}
        # 暂存阶段同时检查与旧注册表、与本批次内部的冲突。
        for registered_tool in registered_tools:
            if registered_tool.name in self._tools or registered_tool.name in additions:
                raise ValueError(f"tool {registered_tool.name!r} is already registered")
            additions[registered_tool.name] = registered_tool
        self._tools.update(additions)

    def get(self, name: str) -> Tool | None:
        """按名称查找 Tool；未知名称返回 ``None``。"""

        return self._tools.get(name)

    def select(self, names: tuple[str, ...] | None = None) -> ToolRegistry:
        """把全部 Tool 或指定有序子集快照到一个新注册表。"""

        selected_names = tuple(self._tools) if names is None else names
        selected: list[Tool] = []
        for name in selected_names:
            registered_tool = self._tools.get(name)
            if registered_tool is None:
                raise KeyError(name)
            selected.append(registered_tool)
        return ToolRegistry(selected)

    def specs(self) -> list[dict[str, Any]]:
        """以稳定名称顺序生成 OpenAI-compatible Tool schema。"""

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
    """构造包含 Phi 内置 Tool 的确定性注册表。"""

    from phi.tools.builtin import BUILTIN_TOOLS

    return ToolRegistry(BUILTIN_TOOLS)


def _mutable_json(value: Any) -> Any:
    """把内部不可变 schema 递归复制成可序列化的普通容器。"""

    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(item) for item in value]
    return value
