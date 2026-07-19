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
    source_path: Path
    reason: str


@dataclass(frozen=True)
class ParsedInstructionFile:
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
    candidates: list[Path] = []
    diagnostics: list[InstructionDiscoveryDiagnostic] = []

    def walk(directory: Path, inherited_rules: tuple[IgnoreRule, ...]) -> None:
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
        if (
            not package_source.is_symlink()
            and package_source.is_file()
            and not is_ignored(package_source, is_directory=False, rules=rules)
        ):
            candidates.append(package_source)
            return

        try:
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
    absolute_path = path.absolute()
    return any(component.is_symlink() for component in (absolute_path, *absolute_path.parents))


def is_ignored(
    path: Path,
    *,
    is_directory: bool,
    rules: tuple[IgnoreRule, ...],
) -> bool:
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
            ignored = match.include
    return ignored
