import json
from pathlib import Path

import pytest

from phi.mcp import (
    McpConfig,
    McpConfigError,
    McpServerConfig,
    load_mcp_config,
    load_merged_mcp_config,
    save_mcp_config,
)


async def test_missing_configuration_loads_as_an_empty_source(tmp_path: Path) -> None:
    config = await load_mcp_config(tmp_path / "missing.json")

    assert config == McpConfig()
    assert config.servers == {}


async def test_project_configuration_replaces_complete_global_server_definitions(
    tmp_path: Path,
) -> None:
    global_path = tmp_path / "global.json"
    project_path = tmp_path / "project.json"
    global_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "global-only": {"command": "global", "args": ["one"]},
                    "shared": {
                        "command": "global-shared",
                        "args": ["global-argument"],
                        "env": {"GLOBAL_ONLY": "present"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    project_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared": {"command": "project-shared"},
                    "disabled": {"command": "unused", "enabled": False},
                }
            }
        ),
        encoding="utf-8",
    )

    config = await load_merged_mcp_config(global_path, project_path)

    assert set(config.servers) == {"global-only", "shared", "disabled"}
    assert config.servers["global-only"].args == ("one",)
    assert config.servers["shared"].command == "project-shared"
    assert config.servers["shared"].args == ()
    assert config.servers["shared"].env == {}
    assert config.servers["disabled"].enabled is False


@pytest.mark.parametrize(
    ("content", "expected_reason"),
    [
        ("{not-json", "invalid JSON"),
        (
            json.dumps(
                {
                    "mcpServers": {
                        "bad": {
                            "command": "server",
                            "env": {"TOKEN": ["do-not-disclose-this-secret"]},
                        }
                    }
                }
            ),
            "invalid MCP configuration",
        ),
        (json.dumps({"mcpServers": {"bad": {"command": "   "}}}), "command"),
    ],
)
async def test_invalid_present_configuration_raises_a_redacted_typed_error(
    tmp_path: Path,
    content: str,
    expected_reason: str,
) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(McpConfigError) as caught:
        await load_mcp_config(path)

    assert caught.value.diagnostic.source_path == path
    assert expected_reason in caught.value.diagnostic.reason
    assert "do-not-disclose-this-secret" not in str(caught.value)


async def test_unreadable_present_source_is_not_treated_as_missing(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.mkdir()

    with pytest.raises(McpConfigError) as caught:
        await load_mcp_config(path)

    assert caught.value.diagnostic.source_path == path
    assert "cannot read MCP configuration" in caught.value.diagnostic.reason


async def test_save_creates_parent_and_writes_deterministic_round_trippable_json(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested" / "mcp.json"
    config = McpConfig(
        mcpServers={
            "zeta": McpServerConfig(
                command="zeta-server",
                args=("--mode", "local"),
                env={"TOKEN": "not-in-repr"},
            ),
            "alpha": McpServerConfig(command="alpha-server", enabled=False),
        }
    )

    await save_mcp_config(path, config)

    assert path.read_text(encoding="utf-8") == (
        "{\n"
        '  "mcpServers": {\n'
        '    "alpha": {\n'
        '      "args": [],\n'
        '      "command": "alpha-server",\n'
        '      "enabled": false,\n'
        '      "env": {}\n'
        "    },\n"
        '    "zeta": {\n'
        '      "args": [\n'
        '        "--mode",\n'
        '        "local"\n'
        "      ],\n"
        '      "command": "zeta-server",\n'
        '      "enabled": true,\n'
        '      "env": {\n'
        '        "TOKEN": "not-in-repr"\n'
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    assert await load_mcp_config(path) == config
    assert "not-in-repr" not in repr(config)
    assert list(path.parent.glob(".mcp.json.*.tmp")) == []
