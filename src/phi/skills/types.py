from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


@dataclass(frozen=True)
class Skill:
    """One validated, fully loaded Agent Skill instruction resource."""

    name: str
    description: str
    content: str
    source_path: Path
    disable_model_invocation: bool = False


@dataclass(frozen=True)
class SkillDiagnostic:
    """Actionable warning produced while tolerantly discovering Skills."""

    source_path: Path
    reason: str

    def __str__(self) -> str:
        return f"{self.source_path}: {self.reason}"


@dataclass(frozen=True)
class SkillDiscovery:
    """Immutable effective Skill collection plus deterministic diagnostics."""

    skills: Mapping[str, Skill]
    diagnostics: tuple[SkillDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "skills", MappingProxyType(dict(self.skills)))
