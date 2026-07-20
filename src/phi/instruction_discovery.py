"""提供 Skills 与 Agent Definitions 共用的安全 Markdown 发现和解析机制。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pathspec import GitIgnoreSpec

PRUNED_DIRECTORIES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
type IgnoreRule = tuple[Path, GitIgnoreSpec]


@dataclass(frozen=True)
class InstructionDiscoveryDiagnostic:
    """扫描某个候选路径时产生的非致命诊断。"""

    source_path: Path
    reason: str


@dataclass(frozen=True)
class ParsedInstructionFile:
    """解析后的 YAML frontmatter 与 Markdown 正文。"""

    frontmatter: dict[Any, Any]
    name: str
    description: str
    content: str


def parse_instruction_file(
    source_path: Path,
    *,
    resource_label: str,
    package_filename: str,
    name_pattern: re.Pattern[str],
) -> ParsedInstructionFile:
    """解析并验证一个文件型或目录包型指令资源。"""

    text = source_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{resource_label} frontmatter must start with ---")
    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing_index is None:
        raise ValueError(f"{resource_label} frontmatter must end with ---")
    frontmatter: Any = yaml.safe_load("".join(lines[1:closing_index]))
    if not isinstance(frontmatter, dict):
        raise ValueError(f"{resource_label} frontmatter must be a mapping")

    name = frontmatter.get("name")
    if not isinstance(name, str) or name_pattern.fullmatch(name) is None:
        raise ValueError(
            f"{resource_label} name must be 1-64 lowercase ASCII letters, digits, or single hyphens"
        )
    expected_name = (
        source_path.parent.name if source_path.name == package_filename else source_path.stem
    )
    # frontmatter 名称必须由文件系统身份决定，防止一个资源冒充另一个资源。
    if name != expected_name:
        raise ValueError(
            f"{resource_label} name {name!r} must match filesystem identity {expected_name!r}"
        )

    description = frontmatter.get("description")
    if not isinstance(description, str):
        raise ValueError(f"{resource_label} description must be text")
    description = description.strip()
    if not 1 <= len(description) <= 1024:
        raise ValueError(f"{resource_label} description must contain 1-1024 characters")
    return ParsedInstructionFile(
        frontmatter=frontmatter,
        name=name,
        description=description,
        content="".join(lines[closing_index + 1 :]),
    )


def initial_ignore_rules(
    ignore_root: Path | None,
    discovery_root: Path,
) -> tuple[tuple[IgnoreRule, ...], tuple[InstructionDiscoveryDiagnostic, ...]]:
    """收集从 ignore 根到发现根沿途生效的 ``.gitignore`` 规则。"""

    if ignore_root is None:
        return (), ()
    try:
        relative_root = discovery_root.relative_to(ignore_root)
    except ValueError:
        return (), ()

    rules: list[IgnoreRule] = []
    diagnostics: list[InstructionDiscoveryDiagnostic] = []
    directory = ignore_root
    for component in relative_root.parts:
        # 符号链接目录会让 ignore 规则的归属边界变得不可信，因此停止继承。
        if has_symlink_component(directory):
            break
        ignore_path = directory / ".gitignore"
        if not ignore_path.is_symlink() and ignore_path.is_file():
            try:
                spec = GitIgnoreSpec.from_lines(
                    ignore_path.read_text(encoding="utf-8").splitlines()
                )
            except (OSError, UnicodeError, ValueError) as error:
                diagnostics.append(
                    InstructionDiscoveryDiagnostic(
                        ignore_path,
                        f"invalid ignore rules: {error}",
                    )
                )
            else:
                rules.append((directory, spec))

        next_directory = directory / component
        # 若祖先规则已经忽略下一级，发现根没有必要继续继承更深规则。
        if is_ignored(next_directory, is_directory=True, rules=tuple(rules)):
            break
        directory = next_directory
    return tuple(rules), tuple(diagnostics)


def candidate_paths(
    root: Path,
    *,
    package_filename: str,
    resource_label: str,
    initial_rules: tuple[IgnoreRule, ...],
) -> tuple[tuple[Path, ...], tuple[InstructionDiscoveryDiagnostic, ...]]:
    """确定性扫描 Markdown 资源，同时应用嵌套 ignore 与剪枝规则。"""

    candidates: list[Path] = []
    diagnostics: list[InstructionDiscoveryDiagnostic] = []

    def walk(directory: Path, inherited_rules: tuple[IgnoreRule, ...]) -> None:
        """递归扫描一个真实目录，并向子目录传递累积 ignore 规则。"""

        rules = inherited_rules
        ignore_path = directory / ".gitignore"
        if not ignore_path.is_symlink() and ignore_path.is_file():
            try:
                ignore_spec = GitIgnoreSpec.from_lines(
                    ignore_path.read_text(encoding="utf-8").splitlines()
                )
            except (OSError, UnicodeError, ValueError) as error:
                diagnostics.append(
                    InstructionDiscoveryDiagnostic(
                        ignore_path,
                        f"invalid ignore rules: {error}",
                    )
                )
            else:
                rules = (*rules, (directory, ignore_spec))

        package_source = directory / package_filename
        # 目录包入口优先：一旦存在，就把整个目录视为一个资源，不再扫描内部 Markdown。
        if (
            not package_source.is_symlink()
            and package_source.is_file()
            and not is_ignored(package_source, is_directory=False, rules=rules)
        ):
            candidates.append(package_source)
            return

        try:
            # 稳定排序让诊断、重复项决策和测试不依赖文件系统顺序。
            entries = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as error:
            diagnostics.append(
                InstructionDiscoveryDiagnostic(
                    directory,
                    f"cannot scan {resource_label} directory: {error}",
                )
            )
            return
        for entry in entries:
            # 不跟随任何符号链接，避免发现根之外的内容伪装成本地资源。
            if entry.name == ".gitignore" or entry.is_symlink():
                continue
            try:
                is_directory = entry.is_dir()
            except OSError as error:
                diagnostics.append(
                    InstructionDiscoveryDiagnostic(
                        entry,
                        f"cannot inspect candidate: {error}",
                    )
                )
                continue
            if is_directory:
                if entry.name in PRUNED_DIRECTORIES or is_ignored(
                    entry,
                    is_directory=True,
                    rules=rules,
                ):
                    continue
                walk(entry, rules)
            elif (
                entry.suffix == ".md"
                and entry.name != package_filename
                and not is_ignored(entry, is_directory=False, rules=rules)
            ):
                candidates.append(entry)

    walk(root, initial_rules)
    return (
        tuple(sorted(candidates, key=lambda path: path.relative_to(root).as_posix())),
        tuple(diagnostics),
    )


def has_symlink_component(path: Path) -> bool:
    """判断路径自身或任一祖先是否为符号链接。"""

    absolute_path = path.absolute()
    return any(component.is_symlink() for component in (absolute_path, *absolute_path.parents))


def is_ignored(
    path: Path,
    *,
    is_directory: bool,
    rules: tuple[IgnoreRule, ...],
) -> bool:
    """按 Git 规则顺序求路径的最终 ignore 状态，支持后续否定规则。"""

    ignored = False
    for base, spec in rules:
        try:
            relative = path.relative_to(base).as_posix()
        except ValueError:
            continue
        if is_directory:
            relative += "/"
        match = spec.check_file(relative)
        if match.include is not None:
            # 后出现的匹配规则覆盖先前状态，保持 gitignore 的 last-match 语义。
            ignored = match.include
    return ignored
