"""提供由有限脚本驱动、无需网络的确定性 Model。"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence

from phi.model.assembler import ResponseAssembler
from phi.model.events import (
    ContentDelta,
    FinishEvent,
    ModelEvent,
    ReasoningDelta,
    ToolCallDelta,
    UsageEvent,
)
from phi.model.types import ModelRequest, ModelResponse

type ScriptEntry = ModelResponse | Sequence[ModelEvent] | Exception


class ScriptedModel:
    """按顺序消费有限响应脚本的确定性离线 Model。"""

    def __init__(self, script: Iterable[ScriptEntry]) -> None:
        """复制脚本到双端队列，并初始化请求观察记录。"""

        self._script = deque(script)
        self.requests: list[ModelRequest] = []

    async def request(self, request: ModelRequest) -> ModelResponse:
        """通过同一流式路径消费一个脚本项并组装完整响应。"""

        # 非流式与流式共用推进语义，避免测试替身出现两套脚本游标。
        assembler = ResponseAssembler()
        async for event in self.request_stream(request):
            assembler.absorb(event)
        return assembler.build()

    async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """记录请求并把下一个脚本项展开为 Model Event。"""

        self.requests.append(request)
        # 脚本耗尽必须显式失败，才能暴露 Harness 意外发出的额外请求。
        if not self._script:
            raise RuntimeError("ScriptedModel response script exhausted")

        entry = self._script.popleft()
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, ModelResponse):
            for event in _events_from_response(entry):
                yield event
            return

        for event in entry:
            yield event


def _events_from_response(response: ModelResponse) -> list[ModelEvent]:
    """把完整 Model Response 转换为可由组装器重放的事件序列。"""

    # 事件顺序模拟真实协议：推理和正文先到，结束信息与 Usage 最后到。
    events: list[ModelEvent] = []
    if response.reasoning is not None:
        events.append(ReasoningDelta(response.reasoning))
    if response.content is not None:
        events.append(ContentDelta(response.content))
    events.extend(
        ToolCallDelta(
            index=index,
            id=tool_call.id,
            name=tool_call.name,
            arguments_fragment=json.dumps(tool_call.arguments),
        )
        for index, tool_call in enumerate(response.tool_calls)
    )
    events.append(FinishEvent(response.finish_reason, response.raw))
    if response.usage is not None:
        events.append(UsageEvent(response.usage, response.raw))
    return events
