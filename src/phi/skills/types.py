"""Agent Skill 发现结果所使用的可信内存值类型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


@dataclass(frozen=True)
class Skill:
    """一个经过校验且已完整加载的 Agent Skill 指令资源。"""

    name: str
    description: str
    content: str
    source_path: Path
    disable_model_invocation: bool = False


@dataclass(frozen=True)
class SkillDiagnostic:
    """容错发现 Skills 时产生的一条可操作警告。"""

    source_path: Path
    reason: str

    def __str__(self) -> str:
        """以“来源路径: 原因”的稳定形式呈现诊断。"""

        return f"{self.source_path}: {self.reason}"


@dataclass(frozen=True)
class SkillDiscovery:
    """不可变的有效 Skill 集合及顺序确定的诊断。"""

    skills: Mapping[str, Skill]
    diagnostics: tuple[SkillDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        """复制并冻结映射，避免发现结果被外部修改。"""

        object.__setattr__(self, "skills", MappingProxyType(dict(self.skills)))
