from __future__ import annotations

import json
import shlex
from pathlib import Path

from phi.environment import ConfinedEnvironment
from phi.model import ToolCall
from phi.tools import (
    BYPASS_MODE,
    ApprovalClass,
    ApprovalMode,
    ApprovalRule,
    RuleBasedApprovalPolicy,
    RuleDecision,
    ToolDispatcher,
    build_default_registry,
)


def _dispatcher(root: Path, *, default_timeout_seconds: float = 30) -> ToolDispatcher:
    environment = ConfinedEnvironment(root)
    return ToolDispatcher(
        build_default_registry(),
        RuleBasedApprovalPolicy(BYPASS_MODE),
        trusted_values={
            "filesystem": environment.filesystem,
            "shell": environment.shell,
        },
        default_timeout_seconds=default_timeout_seconds,
    )


async def test_bash_preserves_process_output_status_and_workspace_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await _dispatcher(workspace).dispatch(
        ToolCall(
            id="bash-1",
            name="bash",
            arguments={
                "command": "pwd; printf output; printf error >&2; exit 7",
            },
        )
    )

    assert result.error is None
    output = json.loads(result.output)
    assert output == {
        "exit_code": 7,
        "stderr": "error",
        "stdout": f"{workspace.resolve()}\noutput",
    }


async def test_bash_timeout_override_returns_a_tool_result(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await _dispatcher(workspace).dispatch(
        ToolCall(
            id="bash-timeout",
            name="bash",
            arguments={"command": "sleep 1", "timeout": 0.05},
        )
    )

    assert result.error is not None
    assert result.error.startswith("execution_timeout:")


async def test_bash_is_explicitly_not_path_confined(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    command = f"printf escaped > {shlex.quote(str(outside))}"

    result = await _dispatcher(workspace).dispatch(
        ToolCall(id="bash-unconfined", name="bash", arguments={"command": command})
    )

    assert result.error is None
    assert outside.read_text() == "escaped"


async def test_bash_default_overrides_a_short_dispatcher_timeout(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await _dispatcher(workspace, default_timeout_seconds=0.01).dispatch(
        ToolCall(
            id="bash-default-timeout",
            name="bash",
            arguments={"command": "sleep 0.05; printf done"},
        )
    )

    assert result.error is None
    assert json.loads(result.output)["stdout"] == "done"


async def test_bash_launch_validation_failure_is_a_tool_result(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await _dispatcher(workspace).dispatch(
        ToolCall(id="bash-invalid", name="bash", arguments={"command": "bad\0command"})
    )

    assert result.error is not None
    assert result.error.startswith("execution_invalid:")


async def test_bash_has_a_120_second_default_and_is_unconfined(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = build_default_registry()
    bash_spec = next(spec for spec in registry.specs() if spec["function"]["name"] == "bash")
    mode = ApprovalMode(
        name="deny-unconfined",
        rules=(ApprovalRule("*", ApprovalClass.UNCONFINED, RuleDecision.DENY),),
        on_unmatched=RuleDecision.ALLOW,
    )
    environment = ConfinedEnvironment(workspace)
    dispatcher = ToolDispatcher(
        registry,
        RuleBasedApprovalPolicy(mode),
        trusted_values={"shell": environment.shell},
    )

    result = await dispatcher.dispatch(
        ToolCall(id="bash-class", name="bash", arguments={"command": "printf denied"})
    )

    assert bash_spec["function"]["parameters"]["properties"]["timeout"]["default"] == 120
    assert "unconfined" in bash_spec["function"]["description"]
    assert result.error == "approval_denied: bash"


async def test_bash_rejects_a_non_finite_requested_timeout(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await _dispatcher(workspace).dispatch(
        ToolCall(
            id="bash-infinite",
            name="bash",
            arguments={"command": "printf no", "timeout": float("inf")},
        )
    )

    assert result.error is not None
    assert result.error.startswith("invalid_arguments:")
