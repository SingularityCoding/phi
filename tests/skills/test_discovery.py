from pathlib import Path

import pytest

from phi.skills import Skill, discover_skills


def test_directory_skill_is_loaded_from_its_frontmatter_and_body(tmp_path: Path) -> None:
    global_root = tmp_path / "global"
    project_root = tmp_path / "project"
    source = project_root / "explain" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\n"
        "name: explain\n"
        "description: Explain a difficult concept.\n"
        "---\n"
        "Use a concrete worked example.\n",
        encoding="utf-8",
    )

    discovery = discover_skills(global_root=global_root, project_root=project_root)

    assert tuple(discovery.skills) == ("explain",)
    assert discovery.skills["explain"] == Skill(
        name="explain",
        description="Explain a difficult concept.",
        content="Use a concrete worked example.\n",
        source_path=source,
    )
    assert discovery.diagnostics == ()


def test_standalone_skill_is_discovered_recursively(tmp_path: Path) -> None:
    global_root = tmp_path / "global"
    project_root = tmp_path / "project"
    source = global_root / "writing" / "summarize.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\n"
        "name: summarize\n"
        "description: Summarize supplied prose.\n"
        "---\n"
        "Preserve important qualifications.\n",
        encoding="utf-8",
    )

    discovery = discover_skills(global_root=global_root, project_root=project_root)

    assert discovery.skills["summarize"].source_path == source
    assert discovery.skills["summarize"].content == "Preserve important qualifications.\n"


def test_malformed_skill_is_diagnosed_without_blocking_valid_skills(tmp_path: Path) -> None:
    global_root = tmp_path / "global"
    project_root = tmp_path / "project"
    global_root.mkdir()
    malformed = global_root / "broken.md"
    malformed.write_text("No frontmatter.\n", encoding="utf-8")
    valid = global_root / "valid.md"
    valid.write_text(
        "---\nname: valid\ndescription: Still available.\n---\nValid body.\n",
        encoding="utf-8",
    )

    discovery = discover_skills(global_root=global_root, project_root=project_root)

    assert tuple(discovery.skills) == ("valid",)
    assert len(discovery.diagnostics) == 1
    assert discovery.diagnostics[0].source_path == malformed
    assert "frontmatter" in discovery.diagnostics[0].reason


@pytest.mark.parametrize(
    "name",
    ["Uppercase", "-leading", "trailing-", "two--hyphens", "has_underscore", "a" * 65],
)
def test_skill_names_follow_the_bounded_safe_identifier_contract(
    tmp_path: Path,
    name: str,
) -> None:
    global_root = tmp_path / "global"
    global_root.mkdir()
    source = global_root / f"{name}.md"
    source.write_text(
        f"---\nname: {name}\ndescription: Invalid name.\n---\nBody.\n",
        encoding="utf-8",
    )

    discovery = discover_skills(
        global_root=global_root,
        project_root=tmp_path / "project",
    )

    assert discovery.skills == {}
    assert len(discovery.diagnostics) == 1
    assert "name" in discovery.diagnostics[0].reason


@pytest.mark.parametrize("relative_path", [Path("actual.md"), Path("actual") / "SKILL.md"])
def test_declared_name_must_match_the_skill_filesystem_identity(
    tmp_path: Path,
    relative_path: Path,
) -> None:
    global_root = tmp_path / "global"
    source = global_root / relative_path
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nname: different\ndescription: Mismatched identity.\n---\nBody.\n",
        encoding="utf-8",
    )

    discovery = discover_skills(
        global_root=global_root,
        project_root=tmp_path / "project",
    )

    assert discovery.skills == {}
    assert len(discovery.diagnostics) == 1
    assert "actual" in discovery.diagnostics[0].reason
    assert "different" in discovery.diagnostics[0].reason


@pytest.mark.parametrize(
    "description_yaml",
    ["description: '   '", "description: 7", f"description: {'x' * 1025}"],
)
def test_skill_description_must_be_nonempty_text_within_the_documented_bound(
    tmp_path: Path,
    description_yaml: str,
) -> None:
    global_root = tmp_path / "global"
    global_root.mkdir()
    source = global_root / "bounded.md"
    source.write_text(
        f"---\nname: bounded\n{description_yaml}\n---\nBody.\n",
        encoding="utf-8",
    )

    discovery = discover_skills(
        global_root=global_root,
        project_root=tmp_path / "project",
    )

    assert discovery.skills == {}
    assert len(discovery.diagnostics) == 1
    assert "description" in discovery.diagnostics[0].reason


def test_disable_model_invocation_must_be_an_explicit_boolean(tmp_path: Path) -> None:
    global_root = tmp_path / "global"
    global_root.mkdir()
    invalid = global_root / "invalid.md"
    invalid.write_text(
        "---\n"
        "name: invalid\n"
        "description: Invalid flag.\n"
        "disable-model-invocation: 'false'\n"
        "---\nBody.\n",
        encoding="utf-8",
    )
    disabled = global_root / "disabled.md"
    disabled.write_text(
        "---\n"
        "name: disabled\n"
        "description: User-only Skill.\n"
        "disable-model-invocation: true\n"
        "---\nTrusted body.\n",
        encoding="utf-8",
    )

    discovery = discover_skills(
        global_root=global_root,
        project_root=tmp_path / "project",
    )

    assert tuple(discovery.skills) == ("disabled",)
    assert discovery.skills["disabled"].disable_model_invocation is True
    assert len(discovery.diagnostics) == 1
    assert discovery.diagnostics[0].source_path == invalid
    assert "disable-model-invocation" in discovery.diagnostics[0].reason


def test_discovery_honors_ignore_rules_package_boundaries_and_symlink_bounds(
    tmp_path: Path,
) -> None:
    global_root = tmp_path / "global"
    global_root.mkdir()
    (global_root / ".gitignore").write_text("ignored/\n", encoding="utf-8")

    package = global_root / "package"
    package.mkdir()
    (package / "SKILL.md").write_text(
        "---\nname: package\ndescription: Packaged Skill.\n---\nPackage body.\n",
        encoding="utf-8",
    )
    references = package / "references"
    references.mkdir()
    (references / "notes.md").write_text("Supporting material, not a Skill.\n", encoding="utf-8")

    ignored = global_root / "ignored"
    ignored.mkdir()
    (ignored / "hidden.md").write_text(
        "---\nname: hidden\ndescription: Ignored.\n---\nHidden.\n",
        encoding="utf-8",
    )
    dependency = global_root / "node_modules"
    dependency.mkdir()
    (dependency / "dependency.md").write_text(
        "---\nname: dependency\ndescription: Dependency.\n---\nDependency.\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped.md").write_text(
        "---\nname: escaped\ndescription: Outside.\n---\nOutside.\n",
        encoding="utf-8",
    )
    (global_root / "linked-directory").symlink_to(outside, target_is_directory=True)
    (global_root / "linked.md").symlink_to(outside / "escaped.md")

    discovery = discover_skills(
        global_root=global_root,
        project_root=tmp_path / "project",
    )

    assert tuple(discovery.skills) == ("package",)
    assert discovery.diagnostics == ()


def test_same_scope_collision_keeps_the_first_valid_lexical_candidate(
    tmp_path: Path,
) -> None:
    global_root = tmp_path / "global"
    directory_source = global_root / "alpha" / "SKILL.md"
    directory_source.parent.mkdir(parents=True)
    directory_source.write_text(
        "---\nname: alpha\ndescription: Directory definition.\n---\nDirectory body.\n",
        encoding="utf-8",
    )
    standalone_source = global_root / "alpha.md"
    standalone_source.write_text(
        "---\nname: alpha\ndescription: Standalone definition.\n---\nStandalone body.\n",
        encoding="utf-8",
    )

    discovery = discover_skills(
        global_root=global_root,
        project_root=tmp_path / "project",
    )

    assert discovery.skills["alpha"].source_path == standalone_source
    assert len(discovery.diagnostics) == 1
    assert discovery.diagnostics[0].source_path == directory_source
    assert "collision" in discovery.diagnostics[0].reason
    assert str(standalone_source) in discovery.diagnostics[0].reason


def test_valid_project_skill_overrides_the_global_definition(tmp_path: Path) -> None:
    global_root = tmp_path / "global"
    project_root = tmp_path / "project"
    global_root.mkdir()
    project_root.mkdir()
    global_source = global_root / "shared.md"
    global_source.write_text(
        "---\nname: shared\ndescription: Global.\n---\nGlobal body.\n",
        encoding="utf-8",
    )
    project_source = project_root / "shared.md"
    project_source.write_text(
        "---\nname: shared\ndescription: Project.\n---\nProject body.\n",
        encoding="utf-8",
    )

    discovery = discover_skills(global_root=global_root, project_root=project_root)

    assert discovery.skills["shared"].source_path == project_source
    assert discovery.skills["shared"].content == "Project body.\n"


def test_malformed_project_override_does_not_erase_a_valid_global_skill(
    tmp_path: Path,
) -> None:
    global_root = tmp_path / "global"
    project_root = tmp_path / "project"
    global_root.mkdir()
    project_root.mkdir()
    global_source = global_root / "shared.md"
    global_source.write_text(
        "---\nname: shared\ndescription: Global.\n---\nGlobal body.\n",
        encoding="utf-8",
    )
    malformed = project_root / "shared.md"
    malformed.write_text("Broken project override.\n", encoding="utf-8")

    discovery = discover_skills(global_root=global_root, project_root=project_root)

    assert discovery.skills["shared"].source_path == global_source
    assert len(discovery.diagnostics) == 1
    assert discovery.diagnostics[0].source_path == malformed
