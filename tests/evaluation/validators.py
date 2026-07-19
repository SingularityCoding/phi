from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from phi.harness import RunStatus
from phi.sessions import (
    AssistantMessageEntry,
    ConversationView,
    ToolResultEntry,
    UserMessageEntry,
    redact_text,
)

from .support import (
    WorkspaceDelta,
    WorkspaceSnapshot,
    format_paths,
    validate_workspace_relative_path,
)


@dataclass(frozen=True)
class EvaluationObservation:
    before: WorkspaceSnapshot
    after: WorkspaceSnapshot
    delta: WorkspaceDelta
    run_statuses: tuple[RunStatus, ...]
    conversation: ConversationView
    trace_records: tuple[Mapping[str, object], ...]


class EvaluationValidator(Protocol):
    def validate(self, observation: EvaluationObservation) -> str | None: ...


@dataclass(frozen=True)
class ExactTextFile:
    path: str
    expected: str

    def __post_init__(self) -> None:
        validate_workspace_relative_path(self.path)

    def validate(self, observation: EvaluationObservation) -> str | None:
        content = observation.after.read(self.path)
        if content is None:
            return f"{self.path} was not created"
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return f"{self.path} was not valid UTF-8 text"
        if text != self.expected:
            return f"{self.path} text did not match the required content"
        return None


@dataclass(frozen=True)
class ExactJsonFile:
    path: str
    expected: object

    def __post_init__(self) -> None:
        validate_workspace_relative_path(self.path)

    def validate(self, observation: EvaluationObservation) -> str | None:
        content = observation.after.read(self.path)
        if content is None:
            return f"{self.path} was not created"
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return f"{self.path} is not valid JSON"
        if value != self.expected:
            return f"{self.path} JSON did not match the required structure and facts"
        return None


@dataclass(frozen=True)
class ExactWorkspaceDelta:
    created: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for path in (*self.created, *self.modified, *self.deleted):
            validate_workspace_relative_path(path)

    def validate(self, observation: EvaluationObservation) -> str | None:
        expected_and_observed = (
            ("created", tuple(sorted(self.created)), observation.delta.created),
            ("modified", tuple(sorted(self.modified)), observation.delta.modified),
            ("deleted", tuple(sorted(self.deleted)), observation.delta.deleted),
        )
        failures = [
            f"workspace {label} paths differed: expected {format_paths(expected)}, "
            f"observed {format_paths(observed)}"
            for label, expected, observed in expected_and_observed
            if expected != observed
        ]
        return "; ".join(failures) if failures else None


@dataclass(frozen=True)
class FilesUnchanged:
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        for path in self.paths:
            validate_workspace_relative_path(path)

    def validate(self, observation: EvaluationObservation) -> str | None:
        changed = [
            path
            for path in self.paths
            if observation.before.read(path) is None
            or observation.before.read(path) != observation.after.read(path)
        ]
        if changed:
            return f"protected sentinel files changed or disappeared: {format_paths(changed)}"
        return None


@dataclass(frozen=True)
class DurableUserMessages:
    expected: tuple[str, ...]

    def validate(self, observation: EvaluationObservation) -> str | None:
        observed = tuple(
            entry.content
            for entry in observation.conversation.entries
            if isinstance(entry, UserMessageEntry)
        )
        if observed != self.expected:
            return "durable User messages did not match the ordered requests"
        return None


@dataclass(frozen=True)
class DurableToolOutcome:
    tool_name: str
    arguments: tuple[tuple[str, object], ...] = ()
    error_prefix: str | None = None

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("tool_name must contain non-whitespace text")
        if self.error_prefix is not None and not self.error_prefix:
            raise ValueError("error_prefix must be non-empty when supplied")

    def validate(self, observation: EvaluationObservation) -> str | None:
        entries = observation.conversation.entries
        for entry_index, entry in enumerate(entries):
            if not isinstance(entry, AssistantMessageEntry):
                continue
            result_group: list[ToolResultEntry] = []
            for following_entry in entries[entry_index + 1 :]:
                if not isinstance(following_entry, ToolResultEntry):
                    break
                result_group.append(following_entry)
            for call in entry.tool_calls:
                if call.name != self.tool_name or any(
                    call.arguments.get(key) != value for key, value in self.arguments
                ):
                    continue
                paired_results = [
                    result_entry.result
                    for result_entry in result_group
                    if result_entry.result.call_id == call.id
                ]
                if len(paired_results) != 1:
                    continue
                result = paired_results[0]
                if self.error_prefix is None and result.error is None:
                    return None
                if self.error_prefix is not None and (
                    result.error is not None and result.error.startswith(self.error_prefix)
                ):
                    return None
        return f"durable Conversation View lacked the required {self.tool_name} Tool outcome"


@dataclass(frozen=True)
class DurableRequestSegments:
    expected_requests: tuple[str, ...]

    def validate(self, observation: EvaluationObservation) -> str | None:
        entries = observation.conversation.entries
        user_indexes = [
            index for index, entry in enumerate(entries) if isinstance(entry, UserMessageEntry)
        ]
        observed_requests = tuple(
            entry.content for entry in entries if isinstance(entry, UserMessageEntry)
        )
        if observed_requests != self.expected_requests:
            return "durable request segments did not contain the ordered User messages"
        for request_index, entry_index in enumerate(user_indexes):
            end = user_indexes[request_index + 1] if request_index + 1 < len(user_indexes) else None
            segment = entries[entry_index + 1 : end]
            calls = {
                call.id
                for entry in segment
                if isinstance(entry, AssistantMessageEntry)
                for call in entry.tool_calls
            }
            results = {
                entry.result.call_id for entry in segment if isinstance(entry, ToolResultEntry)
            }
            if (
                not calls
                or not calls.issubset(results)
                or not segment
                or not isinstance(segment[-1], AssistantMessageEntry)
                or segment[-1].tool_calls
            ):
                return (
                    "durable Entries lacked an ordered Assistant/Tool Call/Tool Result segment "
                    f"for request {request_index + 1}"
                )
        return None


@dataclass(frozen=True)
class DurableSingleBranch:
    def validate(self, observation: EvaluationObservation) -> str | None:
        entries = observation.conversation.entries
        if not entries or observation.conversation.leaf_id != entries[-1].id:
            return "durable Conversation View did not end at its selected Session leaf"
        if any(
            current.parent_id != previous.id
            for previous, current in zip(entries, entries[1:], strict=False)
        ):
            return "durable Conversation View was not one ordered Session branch"
        return None


def evaluate_validators(
    observation: EvaluationObservation,
    validators: tuple[EvaluationValidator, ...],
) -> tuple[str, ...]:
    failures: list[str] = []
    for validator in validators:
        try:
            failure = validator.validate(observation)
        except Exception as error:
            failures.append(
                f"validator {type(validator).__name__} failed safely: {redact_text(str(error))}"
            )
        else:
            if failure is not None:
                failures.append(redact_text(failure))
    return tuple(failures)
