from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from phi.bootstrap import HostRuntime, build_runtime_resources
from phi.cli.headless import execute_headless_run
from phi.harness import RunResult, RunStatus
from phi.instructions import PHI_BASE_INSTRUCTIONS
from phi.model import Model, ModelInfo
from phi.sessions import (
    ConversationView,
    SessionHandle,
    SessionStorage,
    materialize_conversation,
    redact_text,
    resume_session,
)
from phi.settings import Settings
from phi.tools import ACCEPT_EDITS_MODE, ApprovalMode, RuleBasedApprovalPolicy

from .support import (
    WorkspaceDelta,
    WorkspaceSnapshot,
    compare_workspace_snapshots,
    format_paths,
    snapshot_workspace,
    validate_workspace_relative_path,
)
from .validators import EvaluationObservation, EvaluationValidator, evaluate_validators

_SCENARIO_NAME = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*\Z")


class EvaluationInfrastructureError(RuntimeError):
    """Scenario setup or observation was uncertain and therefore failed closed."""


@dataclass(frozen=True)
class SeedDirectory:
    path: str

    def __post_init__(self) -> None:
        validate_workspace_relative_path(self.path)


@dataclass(frozen=True)
class SeedFile:
    path: str
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        validate_workspace_relative_path(self.path)


type WorkspaceSeed = SeedDirectory | SeedFile


@dataclass(frozen=True)
class EvaluationRequest:
    text: str
    max_steps: int
    expected_status: RunStatus = RunStatus.COMPLETED

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("evaluation requests must contain non-whitespace text")
        if (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or self.max_steps <= 0
        ):
            raise ValueError("evaluation max_steps must be a positive integer")


@dataclass(frozen=True)
class EvaluationScenario:
    name: str
    seed: tuple[WorkspaceSeed, ...]
    requests: tuple[EvaluationRequest, ...]
    validators: tuple[EvaluationValidator, ...]
    approval_mode: ApprovalMode = ACCEPT_EDITS_MODE

    def __post_init__(self) -> None:
        if _SCENARIO_NAME.fullmatch(self.name) is None:
            raise ValueError("scenario name must use lowercase letters, digits, '-' or '_'")
        if not self.requests:
            raise ValueError("evaluation scenarios require at least one request")


@dataclass(frozen=True)
class EvaluationDependencies:
    settings: Settings
    model: Model
    available_models: tuple[ModelInfo, ...]
    close_callback: Callable[[], Awaitable[object]] | None = None


@dataclass(frozen=True)
class EvaluationRun:
    status: RunStatus
    step_count: int
    output: str | None
    error: str | None


@dataclass(frozen=True)
class EvaluationResult:
    scenario_name: str
    session: SessionHandle
    runs: tuple[EvaluationRun, ...]
    conversation: ConversationView
    trace_records: tuple[Mapping[str, object], ...]
    trace_path: Path
    before: WorkspaceSnapshot
    after: WorkspaceSnapshot
    delta: WorkspaceDelta
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def session_id(self) -> str:
        return self.session.session_id


async def run_evaluation(
    scenario: EvaluationScenario,
    *,
    root: Path,
    dependencies: EvaluationDependencies,
) -> EvaluationResult:
    """Run one scenario through the existing headless Host operation."""

    layout = _create_isolated_layout(root)
    _seed_workspace(layout.workspace, scenario.seed)
    before = snapshot_workspace(layout.workspace)
    settings = dependencies.settings.model_copy(update={"session_dir": layout.sessions})
    resources = await build_runtime_resources(
        layout.workspace,
        base_instructions=PHI_BASE_INSTRUCTIONS,
        approval_policy=RuleBasedApprovalPolicy(scenario.approval_mode),
        global_skill_root=layout.global_skills,
        global_agent_root=layout.global_agents,
        global_mcp_config_path=layout.global_mcp_config,
    )
    runtime = HostRuntime(
        settings=settings,
        model=dependencies.model,
        available_models=dependencies.available_models,
        storage=SessionStorage(layout.sessions),
        resources=resources,
        close_callback=dependencies.close_callback,
    )

    async def use_scenario_runtime(cwd: Path) -> HostRuntime:
        if cwd.resolve(strict=True) != layout.workspace.resolve(strict=True):
            raise EvaluationInfrastructureError("headless operation requested the wrong workspace")
        return runtime

    handle: SessionHandle | None = None
    raw_runs: list[RunResult] = []
    try:
        for request in scenario.requests:
            outcome = await execute_headless_run(
                request.text,
                cwd=layout.workspace,
                runtime_factory=use_scenario_runtime,
                session_id=None,
                session_handle=handle,
                selected_model=None,
                max_steps=request.max_steps,
                close_runtime=False,
            )
            handle = outcome.session
            raw_runs.append(outcome.result)
    finally:
        await runtime.close()

    if handle is None:
        raise EvaluationInfrastructureError("scenario completed without creating a Session")
    after = snapshot_workspace(layout.workspace)
    delta = compare_workspace_snapshots(before, after)
    storage = SessionStorage(layout.sessions)
    resumed = await resume_session(storage, handle.session_id)
    conversation = await materialize_conversation(storage, resumed)
    trace_path = storage.trace_path(handle.session_id)
    trace_records = _load_trace_records(trace_path)
    runs = tuple(
        EvaluationRun(
            status=result.status,
            step_count=len(result.steps),
            output=None if result.output is None else redact_text(result.output),
            error=None if result.error is None else redact_text(str(result.error)),
        )
        for result in raw_runs
    )
    observation = EvaluationObservation(
        before=before,
        after=after,
        delta=delta,
        run_statuses=tuple(run.status for run in runs),
        conversation=conversation,
        trace_records=trace_records,
    )
    failures = [
        f"request {index} expected {request.expected_status.value}, observed {run.status.value}"
        for index, (request, run) in enumerate(zip(scenario.requests, runs, strict=True), start=1)
        if run.status is not request.expected_status
    ]
    terminal_traces = sum(record.get("event_type") == "run_finished" for record in trace_records)
    if terminal_traces != len(runs):
        failures.append(
            f"redacted Trace contained {terminal_traces} terminal Run records for {len(runs)} Runs"
        )
    failures.extend(evaluate_validators(observation, scenario.validators))
    return EvaluationResult(
        scenario_name=scenario.name,
        session=handle,
        runs=runs,
        conversation=conversation,
        trace_records=trace_records,
        trace_path=trace_path,
        before=before,
        after=after,
        delta=delta,
        failures=tuple(failures),
    )


def format_evaluation_failure(result: EvaluationResult) -> str:
    """Render bounded, redacted diagnostics without dumping artifact or provider contents."""

    statuses = ", ".join(run.status.value for run in result.runs)
    failures = "; ".join(result.failures) or "none"
    run_errors = (
        "; ".join(
            f"request {index}: {run.error}"
            for index, run in enumerate(result.runs, start=1)
            if run.error is not None
        )
        or "none"
    )
    delta = (
        f"created={format_paths(result.delta.created)} "
        f"modified={format_paths(result.delta.modified)} "
        f"deleted={format_paths(result.delta.deleted)}"
    )
    return redact_text(
        f"scenario={result.scenario_name}; failures={failures}; Run statuses=[{statuses}]; "
        f"Run errors={run_errors}; workspace delta: {delta}; Session={result.session_id}; "
        f"redacted Trace={result.trace_path}"
    )


@dataclass(frozen=True)
class _EvaluationLayout:
    root: Path
    workspace: Path
    sessions: Path
    global_skills: Path
    global_agents: Path
    global_mcp_config: Path


def _create_isolated_layout(root: Path) -> _EvaluationLayout:
    if root.is_symlink():
        raise EvaluationInfrastructureError("evaluation root must not be a symlink")
    if root.exists() and any(root.iterdir()):
        raise EvaluationInfrastructureError("evaluation root must be fresh and empty")
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    sessions = root / "sessions"
    global_skills = root / "global-skills"
    global_agents = root / "global-agents"
    for directory in (workspace, sessions, global_skills, global_agents):
        directory.mkdir()
    return _EvaluationLayout(
        root=root,
        workspace=workspace,
        sessions=sessions,
        global_skills=global_skills,
        global_agents=global_agents,
        global_mcp_config=root / "global-mcp.json",
    )


def _seed_workspace(workspace: Path, seed: tuple[WorkspaceSeed, ...]) -> None:
    seen: set[str] = set()
    for item in seed:
        if item.path in seen:
            raise EvaluationInfrastructureError(f"duplicate seed path: {item.path}")
        seen.add(item.path)
        target = workspace.joinpath(*item.path.split("/"))
        if isinstance(item, SeedDirectory):
            try:
                target.mkdir()
            except OSError as error:
                raise EvaluationInfrastructureError(
                    f"could not create seed directory: {item.path}"
                ) from error
            continue
        if not target.parent.is_dir():
            raise EvaluationInfrastructureError(
                f"seed file parent must be declared first: {item.path}"
            )
        try:
            target.write_bytes(item.content)
        except OSError as error:
            raise EvaluationInfrastructureError(
                f"could not create seed file: {item.path}"
            ) from error


def _load_trace_records(path: Path) -> tuple[Mapping[str, object], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise EvaluationInfrastructureError("redacted Trace was not readable") from error
    records: list[Mapping[str, object]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise EvaluationInfrastructureError("redacted Trace contained invalid JSON") from error
        if not isinstance(record, dict):
            raise EvaluationInfrastructureError("redacted Trace record was not an object")
        records.append(record)
    return tuple(records)
