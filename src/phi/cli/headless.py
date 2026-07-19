from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from phi.bootstrap import HostRuntime, model_config_from_settings
from phi.cli.model_selection import (
    require_explicit_model_id,
    resolve_available_model,
)
from phi.harness import EventEmitter, RunEvent, RunResult
from phi.sessions import SessionHandle, create_session, resume_session, select_model, send_message


@dataclass(frozen=True)
class HeadlessOutcome:
    session: SessionHandle
    result: RunResult

    @property
    def session_id(self) -> str:
        return self.session.session_id


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
    session_handle: SessionHandle | None = None,
    close_runtime: bool = True,
    events: EventEmitter[RunEvent] | None = None,
    report_session: TextReporter | None = None,
    report_diagnostic: TextReporter | None = None,
) -> HeadlessOutcome:
    """Resolve one durable Session and delegate one bounded request to its service.

    A caller that owns a longer Host lifetime may supply the current immutable Session handle and
    defer runtime closure. The ordinary CLI path owns and closes the runtime for its single Run.
    """

    if not task.strip():
        raise ValueError("TASK must contain non-whitespace text")
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
        raise ValueError("max_steps must be a positive integer")
    if session_id is not None and session_handle is not None:
        raise ValueError("session_id and session_handle are mutually exclusive")

    runtime = await runtime_factory(cwd)
    if not isinstance(runtime, HostRuntime):
        raise TypeError("runtime factory must return HostRuntime")
    try:
        reported_diagnostics: set[str] = set()

        def report_new_diagnostics(diagnostics: tuple[object, ...]) -> None:
            if report_diagnostic is None:
                return
            for diagnostic in diagnostics:
                text = str(diagnostic)
                if text not in reported_diagnostics:
                    reported_diagnostics.add(text)
                    report_diagnostic(text)

        model_config_from_settings(runtime.settings)
        report_new_diagnostics(runtime.resources.diagnostics)

        handle = session_handle
        if handle is None and session_id is not None:
            handle = await resume_session(runtime.storage, session_id)
        if handle is not None:
            report_new_diagnostics(handle.diagnostics)
        explicit_model = (
            None if selected_model is None else require_explicit_model_id(selected_model)
        )
        resumed_model = None if handle is None else handle.metadata.model
        effective_model, model_info = resolve_available_model(
            explicit_model,
            resumed_model,
            runtime.settings.default_model,
            available_models=runtime.available_models,
            missing_message=("no Model was selected; configure PHI_DEFAULT_MODEL or pass --model"),
        )

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
        report_new_diagnostics(handle.diagnostics)
        return HeadlessOutcome(handle, result)
    finally:
        if close_runtime:
            await runtime.close()
