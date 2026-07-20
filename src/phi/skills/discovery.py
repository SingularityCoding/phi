"""从全局与项目目录发现、校验并合并 Agent Skills。"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from phi.instruction_discovery import (
    IgnoreRule,
    candidate_paths,
    has_symlink_component,
    initial_ignore_rules,
    is_ignored,
    parse_instruction_file,
)
from phi.skills.types import Skill, SkillDiagnostic, SkillDiscovery

_SKILL_NAME_PATTERN = re.compile(r"(?=.{1,64}\Z)[a-z0-9]+(?:-[a-z0-9]+)*")


def discover_skills(
    *,
    global_root: Path,
    project_root: Path,
    project_ignore_root: Path | None = None,
) -> SkillDiscovery:
    """从配置的根目录发现最终生效的全局与项目 Skills。

    项目中同名 Skill 覆盖全局定义；任一无效文件只形成诊断，不会阻断其余
    Skills 的加载。
    """

    # 两个根目录分别容错扫描，最后用字典覆盖表达“项目优先”的确定性规则。
    global_skills, global_diagnostics = _discover_root(global_root)
    project_rules, ignore_diagnostics = initial_ignore_rules(project_ignore_root, project_root)
    project_skills, project_diagnostics = _discover_root(
        project_root,
        initial_rules=project_rules,
    )
    return SkillDiscovery(
        skills={**global_skills, **project_skills},
        diagnostics=(
            *global_diagnostics,
            *(SkillDiagnostic(item.source_path, item.reason) for item in ignore_diagnostics),
            *project_diagnostics,
        ),
    )


def _discover_root(
    root: Path,
    *,
    initial_rules: tuple[IgnoreRule, ...] = (),
) -> tuple[dict[str, Skill], tuple[SkillDiagnostic, ...]]:
    """容错扫描一个 Skill 根目录并返回有效定义与诊断。"""

    # 不跟随目录链上的符号链接，也不读取被 ignore 规则排除的根目录。
    if (
        has_symlink_component(root)
        or not root.is_dir()
        or is_ignored(root, is_directory=True, rules=initial_rules)
    ):
        return {}, ()
    discovered: dict[str, Skill] = {}
    source_paths, candidate_diagnostics = candidate_paths(
        root,
        package_filename="SKILL.md",
        resource_label="Skill",
        initial_rules=initial_rules,
    )
    diagnostics = [SkillDiagnostic(item.source_path, item.reason) for item in candidate_diagnostics]
    for source_path in source_paths:
        # 单个 Skill 的解析失败属于局部错误，批量发现仍继续处理后续候选。
        try:
            skill = _load_skill(source_path)
        except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, ValueError) as error:
            diagnostics.append(SkillDiagnostic(source_path, str(error)))
            continue
        existing = discovered.get(skill.name)
        if existing is not None:
            # 同一根内按候选路径顺序保留首个有效定义，避免结果依赖哈希顺序。
            diagnostics.append(
                SkillDiagnostic(
                    source_path,
                    f"Skill name collision; first valid definition is {existing.source_path}",
                )
            )
            continue
        discovered[skill.name] = skill
    return discovered, tuple(diagnostics)


def _load_skill(source_path: Path) -> Skill:
    """读取并严格校验一个候选 Skill 文件。"""

    parsed = parse_instruction_file(
        source_path,
        resource_label="Skill",
        package_filename="SKILL.md",
        name_pattern=_SKILL_NAME_PATTERN,
    )
    disable_model_invocation = parsed.frontmatter.get("disable-model-invocation", False)
    # 使用 type(...) 而非 isinstance(...)，避免把整数 0/1 当作布尔配置接受。
    if type(disable_model_invocation) is not bool:
        raise ValueError("Skill disable-model-invocation must be a boolean")
    return Skill(
        name=parsed.name,
        description=parsed.description,
        content=parsed.content,
        source_path=source_path,
        disable_model_invocation=disable_model_invocation,
    )
