from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from phi.bootstrap import HostConfigurationError, HostRuntime, model_config_from_settings
from phi.cli.model_selection import resolve_available_model
from phi.model import ModelConfig, ModelInfo
from phi.sessions import (
    ContextInspection,
    SessionHandle,
    SessionStorage,
    inspect_context,
    list_session_handles,
    redact_text,
    resume_session,
)
from phi.settings import Settings


class ContextSelectionError(ValueError):
    """A Context inspection target or effective Model could not be resolved."""


type RuntimeFactory = Callable[[Path], Awaitable[HostRuntime]]
type ModelDiscovery = Callable[[ModelConfig], Awaitable[list[ModelInfo]]]
type SettingsFactory = Callable[[], Settings]


@dataclass(frozen=True)
class ContextCommandOutcome:
    handle: SessionHandle
    model_id: str
    inspection: ContextInspection
    diagnostics: tuple[str, ...]

    def to_document(self) -> dict[str, Any]:
        context = self.inspection.context
        request = self.inspection.request
        metadata = self.handle.metadata
        return {
            "schema_version": 1,
            "session": {
                "id": metadata.id,
                "name": metadata.name,
                "leaf_id": metadata.leaf_id,
                "origin": metadata.origin,
                "parent_session_id": metadata.parent_session_id,
                "fork_point_entry_id": metadata.fork_point_entry_id,
            },
            "model": self.model_id,
            "context": {
                "system_prompt": context.system_prompt,
                "tools": list(context.tools),
                "messages": list(context.messages),
                "dropped_summary": context.dropped_summary,
            },
            "model_request": {
                "messages": request.messages,
                "tools": request.tools,
                "model": request.model,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
            },
            "character_counts": dict(self.inspection.character_counts),
            "token_estimate": {
                "tokens": self.inspection.estimate.tokens,
                "local_tokens": self.inspection.estimate.local_tokens,
                "used_provider_anchor": self.inspection.estimate.used_provider_anchor,
            },
            "input_limits": {
                "effective": self.inspection.effective_input_limit,
                "safe": self.inspection.safe_prompt_limit,
            },
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: Literal["PASS", "FAIL", "SKIP"]
    detail: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def healthy(self) -> bool:
        return all(check.status == "PASS" for check in self.checks)


async def select_context_session(
    storage: SessionStorage,
    session_id: str | None,
) -> SessionHandle:
    """Select an explicit Session or the deterministic most-recent Session."""

    if session_id is not None:
        return await resume_session(storage, session_id)
    handles = await list_session_handles(storage)
    if not handles:
        raise ContextSelectionError("No Sessions found; pass --session after creating one")
    return max(handles, key=lambda handle: (handle.metadata.updated_at, handle.session_id))


async def execute_context_inspection(
    *,
    cwd: Path,
    runtime_factory: RuntimeFactory,
    selected_session: SessionHandle,
) -> ContextCommandOutcome:
    """Build the same cwd-scoped Context as a Run without invoking the Model."""

    runtime = await runtime_factory(cwd)
    if not isinstance(runtime, HostRuntime):
        raise TypeError("runtime factory must return HostRuntime")
    try:
        handle = await resume_session(runtime.storage, selected_session.session_id)
        model_id, model_info = resolve_available_model(
            handle.metadata.model,
            runtime.settings.default_model,
            available_models=runtime.available_models,
            missing_message=(
                "no Model was selected; configure PHI_DEFAULT_MODEL or select a branch Model"
            ),
        )
        inspection = await inspect_context(
            runtime.storage,
            handle,
            settings=runtime.settings,
            model_info=model_info,
            tools=runtime.resources.tools,
            instructions=runtime.resources.instruction_assembly,
        )
        diagnostics = tuple(
            dict.fromkeys(
                str(item)
                for item in (
                    *runtime.resources.diagnostics,
                    *selected_session.diagnostics,
                    *handle.diagnostics,
                    *inspection.diagnostics,
                )
            )
        )
        return ContextCommandOutcome(handle, model_id, inspection, diagnostics)
    finally:
        await runtime.close()


async def run_doctor(
    *,
    settings_factory: SettingsFactory,
    model_discovery: ModelDiscovery,
) -> DoctorReport:
    """Run bounded Model-boundary diagnostics in prerequisite order."""

    try:
        settings = settings_factory()
        config = model_config_from_settings(settings)
        _validate_doctor_base_url(settings.base_url)
    except Exception as error:
        detail = redact_text(str(error)) or type(error).__name__
        return DoctorReport(
            (
                DoctorCheck("settings", "FAIL", detail),
                DoctorCheck("model-discovery", "SKIP"),
                DoctorCheck("default-model", "SKIP"),
            )
        )

    try:
        models = await model_discovery(config)
    except Exception as error:
        detail = _redact_doctor_error(error, settings)
        return DoctorReport(
            (
                DoctorCheck("settings", "PASS"),
                DoctorCheck("model-discovery", "FAIL", detail),
                DoctorCheck("default-model", "SKIP"),
            )
        )

    default_model = settings.default_model.strip()
    if not default_model:
        default_check = DoctorCheck(
            "default-model",
            "FAIL",
            "PHI_DEFAULT_MODEL is required",
        )
    elif default_model not in {model.id for model in models}:
        default_check = DoctorCheck(
            "default-model",
            "FAIL",
            redact_text(f"Model {default_model!r} is not available"),
        )
    else:
        default_check = DoctorCheck("default-model", "PASS")
    return DoctorReport(
        (
            DoctorCheck("settings", "PASS"),
            DoctorCheck("model-discovery", "PASS"),
            default_check,
        )
    )


def _redact_doctor_error(error: Exception, settings: Settings) -> str:
    detail = str(error)
    api_key = settings.api_key.get_secret_value()
    if api_key:
        detail = detail.replace(api_key, "[REDACTED]")
    return redact_text(detail) or type(error).__name__


def _validate_doctor_base_url(base_url: str) -> None:
    try:
        parsed = httpx.URL(base_url)
    except httpx.InvalidURL:
        raise HostConfigurationError("PHI_BASE_URL must be an absolute HTTP(S) URL") from None
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise HostConfigurationError("PHI_BASE_URL must be an absolute HTTP(S) URL")
