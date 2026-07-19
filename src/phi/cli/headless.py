from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from phi.bootstrap import HostRuntime, model_config_from_settings
from phi.harness import EventEmitter, RunEvent, RunResult
from phi.sessions import create_session, resume_session, select_model, send_message


class ModelResolutionError(ValueError):
    """The requested effective Model cannot be selected from the available catalog."""


@dataclass(frozen=True)
class HeadlessOutcome:
    session_id: str
    result: RunResult


type RuntimeFactory = Callable[[Path], Awaitable[HostRuntime]]
type TextReporter = Callable[[str], object]


async def execute_headless_run(
    task: str,
    *,
    cwd: Path,
    runtime_factory: RuntimeFactory,
    session_id: str | None,
    selected_model: str | None,
    max_steps: int,
    events: EventEmitter[RunEvent] | None = None,
    report_session: TextReporter | None = None,
    report_diagnostic: TextReporter | None = None,
) -> HeadlessOutcome:
    """Resolve one durable Session and delegate one bounded request to its service."""

    if not task.strip():
        raise ValueError("TASK must contain non-whitespace text")
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
        raise ValueError("max_steps must be a positive integer")

    runtime = await runtime_factory(cwd)
    if not isinstance(runtime, HostRuntime):
        raise TypeError("runtime factory must return HostRuntime")
    try:
        model_config_from_settings(runtime.settings)
        if selected_model is not None and not selected_model.strip():
            raise ModelResolutionError("--model must contain non-whitespace text")
        if report_diagnostic is not None:
            for diagnostic in runtime.resources.diagnostics:
                report_diagnostic(str(diagnostic))

        handle = (
            await resume_session(runtime.storage, session_id) if session_id is not None else None
        )
        explicit_model = _optional_text(selected_model)
        resumed_model = None if handle is None else _optional_text(handle.metadata.model)
        default_model = _optional_text(runtime.settings.default_model)
        effective_model = explicit_model or resumed_model or default_model
        if effective_model is None:
            raise ModelResolutionError(
                "no Model was selected; configure PHI_DEFAULT_MODEL or pass --model"
            )
        model_catalog = {info.id: info for info in runtime.available_models}
        model_info = model_catalog.get(effective_model)
        if model_info is None:
            raise ModelResolutionError(f"Model {effective_model!r} is not available")

        if handle is None:
            handle = await create_session(runtime.storage, model=effective_model)
        elif explicit_model is not None or handle.metadata.model is None:
            handle = await select_model(runtime.storage, handle, effective_model)

        if report_session is not None:
            report_session(handle.session_id)
        handle, result = await send_message(
            handle,
            task,
            storage=runtime.storage,
            settings=runtime.settings,
            model=runtime.model,
            model_info=model_info,
            tools=runtime.resources.tools,
            dispatcher=runtime.resources.dispatcher,
            stable_instructions=runtime.resources.stable_instructions,
            max_steps=max_steps,
            events=events,
            lifecycle=runtime.resources.agents,
        )
        return HeadlessOutcome(handle.session_id, result)
    finally:
        await runtime.close()


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
