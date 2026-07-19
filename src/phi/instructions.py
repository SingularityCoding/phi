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
class ProjectInstructions:
    """Stable repository instructions selected from the working-directory root."""

    content: str
    source_path: Path | None


class ProjectInstructionsError(RuntimeError):
    """A selected project-instruction resource could not be loaded safely."""

    def __init__(self, source_path: Path, reason: str) -> None:
        self.source_path = source_path
        super().__init__(f"cannot load Project Instructions from {source_path}: {reason}")


def load_project_instructions(cwd: Path) -> ProjectInstructions:
    """Load root project instructions, preferring AGENTS.md over CLAUDE.md."""

    for filename in ("AGENTS.md", "CLAUDE.md"):
        source_path = cwd / filename
        try:
            content = source_path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            if source_path.is_symlink():
                raise ProjectInstructionsError(source_path, str(error)) from error
            continue
        except UnicodeDecodeError as error:
            raise ProjectInstructionsError(source_path, "content is not valid UTF-8") from error
        except OSError as error:
            raise ProjectInstructionsError(source_path, str(error)) from error
        return ProjectInstructions(content=content, source_path=source_path)
    return ProjectInstructions(content="", source_path=None)
