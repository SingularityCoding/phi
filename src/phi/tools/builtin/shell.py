"""提供明确标记为不受路径 confinement 保护的 Bash Tool。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from phi.environment import ExecutionError, Shell
from phi.tools.dispatcher import ToolFailure
from phi.tools.types import ApprovalClass, Injected, tool

BASH_DEFAULT_TIMEOUT_SECONDS = 120.0


@tool(
    name="bash",
    description=(
        "Run an unconfined shell command from the workspace working directory. "
        "This is not path-confined or operating-system sandboxed."
    ),
    approval_class=ApprovalClass.UNCONFINED,
    timeout_seconds=BASH_DEFAULT_TIMEOUT_SECONDS,
    timeout_parameter="timeout",
)
async def run_bash(
    command: Annotated[str, Field(min_length=1)],
    shell: Injected[Shell],
    timeout: Annotated[float, Field(gt=0, allow_inf_nan=False)] = BASH_DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, int | str] | ToolFailure:
    """从工作区 cwd 执行命令，并把进程结果转换为结构化输出。"""

    result = await shell.exec(command, timeout_seconds=timeout)
    if isinstance(result, ExecutionError):
        return ToolFailure(f"execution_{result.code.value}: {result.message}")
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
