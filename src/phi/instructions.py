"""发现并组装发送给 Model 的稳定基础指令与项目指令。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PHI_BASE_INSTRUCTIONS = """\
You are Phi, an Agent composed from a Model and a Harness.
Follow the user's instructions and the Project Instructions supplied in the Context.
Use Tools for actions and accept that the Harness governs whether and how Tool Calls execute.
Treat the Environment as ground truth: inspect it when needed and verify consequential work.
Never claim that an action was completed unless you performed it and observed the result.
"""


@dataclass(frozen=True)
class InstructionSection:
    """与组装后 prompt 一同保留的可信稳定指令来源。"""

    id: str
    delimiter_label: str | None
    origin: str
    source: str
    content: str

    @property
    def rendered(self) -> str:
        """用可见边界包裹有标签的来源，基础指令则保持原样。"""

        if self.delimiter_label is None:
            return self.content
        ending = "" if self.content.endswith("\n") else "\n"
        label = self.delimiter_label.upper()
        return f"--- BEGIN {label} ---\n{self.content}{ending}--- END {label} ---"

    @property
    def characters(self) -> int:
        """返回原始内容字符数，供 Context 检查界面展示。"""

        return len(self.content)

    @property
    def inclusion(self) -> str:
        """描述该稳定来源在当前 Context 中的包含状态。"""

        return "Stable · included"


@dataclass(frozen=True)
class InstructionAssembly:
    """以同一份 section 结构同时生成稳定 prompt 和可信来源元数据。"""

    sections: tuple[InstructionSection, ...]

    @property
    def stable_instructions(self) -> str:
        """按来源顺序拼接发送给 Model 的稳定指令。"""

        return "\n\n".join(section.rendered for section in self.sections)

    @classmethod
    def from_prompt(cls, prompt: str) -> InstructionAssembly:
        """表示调用方提供的指令，同时不虚构可信文件来源。"""

        sections = (
            InstructionSection(
                id="unattributed",
                delimiter_label=None,
                origin="Unattributed stable instructions",
                source="Application caller",
                content=prompt,
            ),
        )
        return cls(sections if prompt else ())


@dataclass(frozen=True)
class ProjectInstructions:
    """从工作目录根选择的稳定仓库指令及其来源路径。"""

    content: str
    source_path: Path | None


class ProjectInstructionsError(RuntimeError):
    """已选中的项目指令资源无法被安全加载。"""

    def __init__(self, source_path: Path, reason: str) -> None:
        """保留失败来源，并生成面向 Host 的可行动消息。"""

        self.source_path = source_path
        super().__init__(f"cannot load Project Instructions from {source_path}: {reason}")


def load_project_instructions(cwd: Path) -> ProjectInstructions:
    """加载根目录项目指令，``AGENTS.md`` 优先于 ``CLAUDE.md``。"""

    for filename in ("AGENTS.md", "CLAUDE.md"):
        source_path = cwd / filename
        try:
            content = source_path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            # 断裂符号链接是明确选中却无法读取，不能被当作“文件不存在”跳过。
            if source_path.is_symlink():
                raise ProjectInstructionsError(source_path, str(error)) from error
            continue
        except UnicodeDecodeError as error:
            raise ProjectInstructionsError(source_path, "content is not valid UTF-8") from error
        except OSError as error:
            raise ProjectInstructionsError(source_path, str(error)) from error
        return ProjectInstructions(content=content, source_path=source_path)
    # 没有项目指令是合法状态，基础指令仍会进入 Context。
    return ProjectInstructions(content="", source_path=None)
