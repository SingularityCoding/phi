from pathlib import Path

import pytest

from phi.instructions import (
    ProjectInstructions,
    ProjectInstructionsError,
    load_project_instructions,
)


def test_root_agents_instructions_take_precedence_over_claude(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("Follow the repository rules.\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("Fallback rules.\n", encoding="utf-8")

    instructions = load_project_instructions(tmp_path)

    assert instructions == ProjectInstructions(
        content="Follow the repository rules.\n",
        source_path=agents,
    )


def test_root_claude_instructions_are_used_as_the_fallback(tmp_path: Path) -> None:
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("Keep the fallback compatible.\n", encoding="utf-8")

    instructions = load_project_instructions(tmp_path)

    assert instructions == ProjectInstructions(
        content="Keep the fallback compatible.\n",
        source_path=claude,
    )


def test_selected_project_instructions_report_the_path_when_utf8_decoding_fails(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_bytes(b"\xff")

    with pytest.raises(ProjectInstructionsError) as raised:
        load_project_instructions(tmp_path)

    assert raised.value.source_path == agents
    assert str(agents) in str(raised.value)
    assert "UTF-8" in str(raised.value)


def test_project_instructions_are_optional_and_nested_files_are_not_loaded(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "AGENTS.md").write_text("Nested rules.\n", encoding="utf-8")

    instructions = load_project_instructions(tmp_path)

    assert instructions == ProjectInstructions(content="", source_path=None)


def test_unreadable_selected_instruction_resource_does_not_fall_back(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.mkdir()
    (tmp_path / "CLAUDE.md").write_text("Fallback must not hide failure.\n", encoding="utf-8")

    with pytest.raises(ProjectInstructionsError) as raised:
        load_project_instructions(tmp_path)

    assert raised.value.source_path == agents


def test_dangling_selected_instruction_symlink_does_not_fall_back(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.symlink_to(tmp_path / "missing.md")
    (tmp_path / "CLAUDE.md").write_text("Fallback must not hide failure.\n", encoding="utf-8")

    with pytest.raises(ProjectInstructionsError) as raised:
        load_project_instructions(tmp_path)

    assert raised.value.source_path == agents
