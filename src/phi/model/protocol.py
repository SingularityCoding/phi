"""声明 Harness 可依赖的无状态 Model 结构协议。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from phi.model.events import ModelEvent
from phi.model.types import ModelRequest, ModelResponse


@runtime_checkable
class Model(Protocol):
    """定义一次归一化 Model 交互的无状态边界。"""

    async def request(self, request: ModelRequest) -> ModelResponse:
        """执行一次非流式请求并返回完整响应。"""
        ...

    def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """执行一次流式请求并按到达顺序产出 Model Event。"""
        ...
