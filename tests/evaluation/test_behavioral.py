from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from phi.bootstrap import model_config_from_settings
from phi.model import OpenAICompatibleModel, list_available_models
from phi.sessions import redact_text
from phi.settings import Settings

from .runner import (
    EvaluationDependencies,
    EvaluationRequest,
    EvaluationScenario,
    SeedDirectory,
    SeedFile,
    format_evaluation_failure,
    run_evaluation,
)
from .validators import (
    DurableRequestSegments,
    DurableSingleBranch,
    DurableToolOutcome,
    DurableUserMessages,
    ExactJsonFile,
    ExactTextFile,
    ExactWorkspaceDelta,
    FilesUnchanged,
)

RUN_BEHAVIORAL_EVALS = os.getenv("PHI_RUN_BEHAVIORAL_EVALS") == "1"

pytestmark = [
    pytest.mark.behavioral,
    pytest.mark.skipif(
        not RUN_BEHAVIORAL_EVALS,
        reason="set PHI_RUN_BEHAVIORAL_EVALS=1 to run live behavioral evaluations",
    ),
]

MULTI_SOURCE_REQUEST = (
    "Read every file under sources. Create output/manifest.json containing exactly the project, "
    "owner, release, and ordered regions found there. Do not invent records or change other files."
)
SURGICAL_EDIT_REQUEST = (
    "In deployment.md, change only the replicas value from 2 to 3. Preserve all surrounding "
    "content and every unrelated file exactly."
)
STALE_PATH_REQUEST = (
    "First try to read source/current.json, then create output/recovered.json with its marker and "
    "value. The given source path may be stale; after the expected read error, recover by finding "
    "the unique PHI-RECOVERY-731 marker in the workspace."
)
INSTRUCTIONS_SKILL_REQUEST = (
    "Use the artifact-contract Skill and follow Project Instructions to create "
    "output/contract.json. Do not modify the instruction or Skill sources."
)
SESSION_FIRST_REQUEST = 'Create output/plan.json as {"project":"atlas","milestones":["draft"]}.'
SESSION_FOLLOW_UP = (
    "Using the artifact and project from my earlier request, append the ordered milestones review "
    "and ship. Keep the existing draft milestone."
)

BEHAVIORAL_SCENARIOS = (
    EvaluationScenario(
        name="multi-source-artifact",
        seed=(
            SeedDirectory("sources"),
            SeedDirectory("output"),
            SeedFile("sources/project.json", b'{"project":"orion","owner":"Mina"}\n'),
            SeedFile("sources/release.txt", b"release=2026.07\n"),
            SeedFile("sources/regions.txt", b"ap-south\neu-west\n"),
            SeedFile("protected.keep", b"multi-source sentinel\n"),
        ),
        requests=(EvaluationRequest(MULTI_SOURCE_REQUEST, max_steps=8),),
        validators=(
            ExactJsonFile(
                "output/manifest.json",
                {
                    "project": "orion",
                    "owner": "Mina",
                    "release": "2026.07",
                    "regions": ["ap-south", "eu-west"],
                },
            ),
            ExactWorkspaceDelta(created=("output/manifest.json",)),
            FilesUnchanged(
                (
                    "sources/project.json",
                    "sources/release.txt",
                    "sources/regions.txt",
                    "protected.keep",
                )
            ),
        ),
    ),
    EvaluationScenario(
        name="surgical-edit",
        seed=(
            SeedFile(
                "deployment.md",
                b"# Deployment\nregion: eu-west\nreplicas: 2\nnotes: preserve this byte-for-byte\n",
            ),
            SeedFile("unrelated.txt", b"untouched sentinel\n"),
        ),
        requests=(EvaluationRequest(SURGICAL_EDIT_REQUEST, max_steps=6),),
        validators=(
            ExactTextFile(
                "deployment.md",
                "# Deployment\nregion: eu-west\nreplicas: 3\nnotes: preserve this byte-for-byte\n",
            ),
            ExactWorkspaceDelta(modified=("deployment.md",)),
            FilesUnchanged(("unrelated.txt",)),
        ),
    ),
    EvaluationScenario(
        name="stale-path-recovery",
        seed=(
            SeedDirectory("archive"),
            SeedDirectory("output"),
            SeedFile(
                "archive/actual-source.json",
                b'{"marker":"PHI-RECOVERY-731","value":42}\n',
            ),
            SeedFile("unrelated.keep", b"recovery sentinel\n"),
        ),
        requests=(EvaluationRequest(STALE_PATH_REQUEST, max_steps=8),),
        validators=(
            ExactJsonFile(
                "output/recovered.json",
                {"marker": "PHI-RECOVERY-731", "value": 42},
            ),
            ExactWorkspaceDelta(created=("output/recovered.json",)),
            FilesUnchanged(("archive/actual-source.json", "unrelated.keep")),
            DurableToolOutcome(
                "read",
                arguments=(("path", "source/current.json"),),
                error_prefix="file_not_found:",
            ),
        ),
    ),
    EvaluationScenario(
        name="project-instructions-and-skill",
        seed=(
            SeedDirectory(".phi"),
            SeedDirectory(".phi/skills"),
            SeedDirectory(".phi/skills/artifact-contract"),
            SeedDirectory("output"),
            SeedFile(
                "AGENTS.md",
                b"Generated contract JSON must include project_rule with value violet.\n",
            ),
            SeedFile(
                ".phi/skills/artifact-contract/SKILL.md",
                b"""---
name: artifact-contract
description: Define the required deterministic contract artifact.
---
The artifact must include skill_rule with value triangle and format_version with integer value 1.
""",
            ),
        ),
        requests=(EvaluationRequest(INSTRUCTIONS_SKILL_REQUEST, max_steps=8),),
        validators=(
            ExactJsonFile(
                "output/contract.json",
                {
                    "project_rule": "violet",
                    "skill_rule": "triangle",
                    "format_version": 1,
                },
            ),
            ExactWorkspaceDelta(created=("output/contract.json",)),
            FilesUnchanged(("AGENTS.md", ".phi/skills/artifact-contract/SKILL.md")),
            DurableToolOutcome(
                "skill_tool",
                arguments=(("name", "artifact-contract"),),
            ),
        ),
    ),
    EvaluationScenario(
        name="session-continuity",
        seed=(SeedDirectory("output"),),
        requests=(
            EvaluationRequest(SESSION_FIRST_REQUEST, max_steps=6),
            EvaluationRequest(SESSION_FOLLOW_UP, max_steps=6),
        ),
        validators=(
            ExactJsonFile(
                "output/plan.json",
                {"project": "atlas", "milestones": ["draft", "review", "ship"]},
            ),
            ExactWorkspaceDelta(created=("output/plan.json",)),
            DurableUserMessages((SESSION_FIRST_REQUEST, SESSION_FOLLOW_UP)),
            DurableRequestSegments((SESSION_FIRST_REQUEST, SESSION_FOLLOW_UP)),
            DurableSingleBranch(),
        ),
    ),
)


@pytest.fixture
async def live_dependencies() -> AsyncIterator[EvaluationDependencies]:
    settings = Settings()
    if not settings.api_key.get_secret_value().strip() or not settings.default_model.strip():
        pytest.fail(
            "live behavioral evaluations require PHI_API_KEY and PHI_DEFAULT_MODEL",
            pytrace=False,
        )
    try:
        config = model_config_from_settings(settings)
    except ValueError as error:
        pytest.fail(redact_text(str(error)), pytrace=False)
    client = httpx.AsyncClient()
    try:
        try:
            available_models = tuple(await list_available_models(config, client=client))
        except Exception as error:
            pytest.fail(
                f"live Model discovery failed: {redact_text(str(error))}",
                pytrace=False,
            )
        if settings.default_model not in {model.id for model in available_models}:
            pytest.fail(
                f"configured default Model {settings.default_model!r} is not available",
                pytrace=False,
            )
        yield EvaluationDependencies(
            settings=settings,
            model=OpenAICompatibleModel(config, client=client),
            available_models=available_models,
            close_callback=client.aclose,
        )
    finally:
        if not client.is_closed:
            await client.aclose()


@pytest.mark.parametrize(
    "scenario",
    BEHAVIORAL_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
async def test_live_agent_behavior(
    scenario: EvaluationScenario,
    live_dependencies: EvaluationDependencies,
    tmp_path: Path,
) -> None:
    result = await run_evaluation(
        scenario,
        root=tmp_path / scenario.name,
        dependencies=live_dependencies,
    )

    assert result.passed, format_evaluation_failure(result)
