"""把已物化的 Conversation View 投影为一次有限、可信的 Context。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from phi.harness.snapshots import freeze_request
from phi.model import ModelRequest


@dataclass(frozen=True)
class Context:
    """保存用于构造一次普通 Model 请求的有限、可信投影。"""

    system_prompt: str
    tools: tuple[dict[str, Any], ...]
    messages: tuple[dict[str, Any], ...]
    dropped_summary: str | None = None

    def __post_init__(self) -> None:
        """冻结消息和工具的嵌套 wire 值，防止 Context 构造后漂移。"""

        # 借用统一快照逻辑，确保 tuple 外壳内部的 list/dict 也不可被观察者修改。
        snapshot = freeze_request(
            ModelRequest(messages=list(self.messages), tools=list(self.tools))
        )
        object.__setattr__(self, "messages", tuple(snapshot.messages))
        object.__setattr__(self, "tools", tuple(snapshot.tools))

    def to_request(
        self,
        *,
        model: str | None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelRequest:
        """将 Context 转换成一次独立的 Model Request。

        Args:
            model: 本次请求显式选择的 Model ID。
            temperature: 可选采样温度。
            max_tokens: 可选最大输出 token 数。

        Returns:
            与 Context 内部快照不共享可变容器的 Model Request。
        """

        # 稳定指令始终位于首条 system 消息，Compaction 摘要紧随其后。
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
        ]
        if self.dropped_summary is not None:
            messages.append(
                {
                    "role": "system",
                    "content": ("Dropped conversation history summary:\n" + self.dropped_summary),
                }
            )
        # wire 字典需要深拷贝；Model 适配器或调用方不能反向修改 Context。
        messages.extend(deepcopy(list(self.messages)))
        return ModelRequest(
            messages=messages,
            tools=deepcopy(list(self.tools)),
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    @property
    def character_counts(self) -> dict[str, int]:
        """分别返回 Context 各组成部分的字符数，供只读检查界面展示。"""

        return {
            "system_prompt": len(self.system_prompt),
            "dropped_summary": len(self.dropped_summary or ""),
            "messages": len(json.dumps(self.messages, ensure_ascii=False, separators=(",", ":"))),
            "tools": len(json.dumps(self.tools, ensure_ascii=False, separators=(",", ":"))),
        }


def build_context(
    *,
    stable_instructions: str,
    tool_specs: list[dict[str, Any]],
    conversation_messages: list[dict[str, Any]],
    dropped_summary: str | None,
) -> Context:
    """从稳定输入与 Conversation View 消息构造隔离的 Context。

    此函数不访问 Session，也不执行 Compaction；调用方应先完成 Conversation View
    物化与策略选择，从而维持 Harness 对 Sessions 的负依赖规则。
    """

    return Context(
        system_prompt=stable_instructions,
        tools=tuple(deepcopy(tool_specs)),
        messages=tuple(deepcopy(conversation_messages)),
        dropped_summary=dropped_summary,
    )
