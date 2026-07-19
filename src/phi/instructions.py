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
    """Trusted stable-instruction source retained alongside the assembled prompt."""

    id: str
    delimiter_label: str | None
    origin: str
    source: str
    content: str

    @property
    def rendered(self) -> str:
        if self.delimiter_label is None:
            return self.content
        ending = "" if self.content.endswith("\n") else "\n"
        label = self.delimiter_label.upper()
        return f"--- BEGIN {label} ---\n{self.content}{ending}--- END {label} ---"

    @property
    def characters(self) -> int:
        return len(self.content)

    @property
    def inclusion(self) -> str:
        return "Stable · included"


@dataclass(frozen=True)
class InstructionAssembly:
    """Stable prompt and trusted source metadata with one structural source of truth."""

    sections: tuple[InstructionSection, ...]

    @property
    def stable_instructions(self) -> str:
        return "\n\n".join(section.rendered for section in self.sections)

    @classmethod
    def from_prompt(cls, prompt: str) -> InstructionAssembly:
        """Represent caller-supplied instructions without inventing a trusted origin."""

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
