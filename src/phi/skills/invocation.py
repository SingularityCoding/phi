"""区分 Model 与可信用户两条 Skill 调用路径。"""

from __future__ import annotations

from collections.abc import Mapping

from phi.skills.types import Skill
from phi.tools import Tool, ToolFailure, tool


class SkillNotFoundError(LookupError):
    """可信用户调用选择了不存在的精确 Skill 名称。"""

    def __init__(self, name: str) -> None:
        """记录未知 Skill 名称，并构造稳定错误消息。"""

        self.name = name
        super().__init__(f"unknown Skill: {name}")


def model_invocable_skills(skills: Mapping[str, Skill]) -> tuple[Skill, ...]:
    """按稳定名称顺序返回允许 Model 调用的 Skills。"""

    return tuple(
        sorted(
            (skill for skill in skills.values() if not skill.disable_model_invocation),
            key=lambda skill: skill.name,
        )
    )


def render_model_skill_menu(skills: Mapping[str, Skill]) -> str:
    """渲染 Model 可调用 Skills 的精简目录与激活指引。"""

    available = model_invocable_skills(skills)
    if not available:
        return ""
    # 折叠描述中的换行与重复空白，避免元数据无谓占用稳定 system prompt。
    entries = "\n".join(
        f"- `{skill.name}`: {' '.join(skill.description.split())}" for skill in available
    )
    return "Load a Skill by calling `skill_tool` with its exact name.\n" + entries


def build_skill_tool(skills: Mapping[str, Skill]) -> Tool | None:
    """基于已加载集合创建只读的 Model Skill 激活 Tool。

    禁止 Model 调用的 Skill 在闭包建立时就被排除；即使 Model 猜中精确名称，
    也不会从错误结果中泄露其内容。
    """

    available = {skill.name: skill for skill in model_invocable_skills(skills)}
    if not available:
        return None

    @tool(
        name="skill_tool",
        description="Load one Model-invocable Agent Skill by exact name.",
    )
    async def load_skill(name: str) -> str | ToolFailure:
        """按精确名称返回 Model 可调用 Skill 的已加载正文。"""

        skill = available.get(name)
        if skill is None:
            return ToolFailure("skill_unavailable: no Model-invocable Skill has that exact name")
        return skill.content

    return load_skill


def invoke_user_skill(skills: Mapping[str, Skill], name: str) -> str:
    """返回可信用户精确选择的 Skill 正文，包括禁止 Model 调用的 Skill。"""

    skill = skills.get(name)
    if skill is None:
        raise SkillNotFoundError(name)
    return skill.content
