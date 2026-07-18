from __future__ import annotations

from collections.abc import Mapping

from phi.skills.types import Skill
from phi.tools import Tool, ToolFailure, tool


class SkillNotFoundError(LookupError):
    """Trusted user invocation requested an unknown exact Skill name."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"unknown Skill: {name}")


def model_invocable_skills(skills: Mapping[str, Skill]) -> tuple[Skill, ...]:
    """Return Model-visible Skills in stable name order."""

    return tuple(
        sorted(
            (skill for skill in skills.values() if not skill.disable_model_invocation),
            key=lambda skill: skill.name,
        )
    )


def render_model_skill_menu(skills: Mapping[str, Skill]) -> str:
    """Render compact metadata and activation guidance for Model-invocable Skills."""

    available = model_invocable_skills(skills)
    if not available:
        return ""
    entries = "\n".join(
        f"- `{skill.name}`: {' '.join(skill.description.split())}" for skill in available
    )
    return "Load a Skill by calling `skill_tool` with its exact name.\n" + entries


def build_skill_tool(skills: Mapping[str, Skill]) -> Tool | None:
    """Create the read-only Model activation Tool over an already loaded collection."""

    available = {skill.name: skill for skill in model_invocable_skills(skills)}
    if not available:
        return None

    @tool(
        name="skill_tool",
        description="Load one Model-invocable Agent Skill by exact name.",
    )
    async def load_skill(name: str) -> str | ToolFailure:
        skill = available.get(name)
        if skill is None:
            return ToolFailure("skill_unavailable: no Model-invocable Skill has that exact name")
        return skill.content

    return load_skill


def invoke_user_skill(skills: Mapping[str, Skill], name: str) -> str:
    """Return loaded content for an exact trusted user selection, including disabled Skills."""

    skill = skills.get(name)
    if skill is None:
        raise SkillNotFoundError(name)
    return skill.content
