import asyncio
import json
import threading
from collections.abc import AsyncIterator, Coroutine
from pathlib import Path

import pytest

from phi.agents import DelegationContext
from phi.bootstrap import CwdRuntimeBootstrap, build_runtime_resources
from phi.harness import EventBus, Hooks, RunFinished, RunStatus
from phi.model import (
    ContentDelta,
    FinishEvent,
    ModelEvent,
    ModelRequest,
    ModelResponse,
    ScriptedModel,
    ToolCall,
    ToolCallDelta,
)
from phi.sessions import (
    AssistantMessageEntry,
    SessionHandle,
    SessionStorage,
    UserMessageEntry,
    create_session,
    list_sessions,
    materialize_conversation,
    resume_session,
    send_message,
)
from phi.sessions.metadata import SessionMetadataEnvelope
from phi.settings import Settings
from phi.tools import (
    BYPASS_MODE,
    DEFAULT_MODE,
    ApprovalDecision,
    RuleBasedApprovalPolicy,
    Tool,
    tool,
)


class _SelectiveApprovalPolicy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def decide(self, call: ToolCall, tool: Tool) -> ApprovalDecision:
        del call
        self.calls.append(tool.name)
        if tool.name == "bash":
            return ApprovalDecision.DENY
        return ApprovalDecision.ALLOW


def _write_agent_definition(source: Path) -> None:
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "---\n"
        "name: specialist\n"
        "description: Work on a focused task.\n"
        "model: child-model\n"
        "---\n"
        "Follow the specialist instructions.\n",
        encoding="utf-8",
    )


class _DelegationModel:
    def __init__(self) -> None:
        self.parent_calls = 0
        self.child_started = asyncio.Event()
        self.release_child = asyncio.Event()
        self.management_seen = asyncio.Event()
        self.child_requests: list[ModelRequest] = []
        self.check_result: dict[str, object] | None = None
        self.list_result: list[dict[str, object]] | None = None

    async def request(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"ordinary Runs must stream: {request}")

    async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if request.model == "child-model":
            self.child_requests.append(request)
            self.child_started.set()
            await self.release_child.wait()
            yield ContentDelta("child result")
            yield FinishEvent("stop", {})
            return

        self.parent_calls += 1
        if self.parent_calls == 1:
            yield ToolCallDelta(
                index=0,
                id="spawn-1",
                name="spawn_agent",
                arguments_fragment=json.dumps(
                    {"task": "isolated task", "agent_type": "specialist"}
                ),
            )
            yield FinishEvent("tool_calls", {})
            return

        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in request.messages
            if message["role"] == "tool"
        }
        if self.parent_calls == 2:
            agent_id = json.loads(tool_messages["spawn-1"])["agent_id"]
            for index, (call_id, name, arguments) in enumerate(
                (
                    ("check-1", "check_agent", {"agent_id": agent_id}),
                    (
                        "check-timeout",
                        "check_agent",
                        {"agent_id": agent_id, "timeout_seconds": 0.01},
                    ),
                    ("list-1", "list_agents", {}),
                )
            ):
                yield ToolCallDelta(
                    index=index,
                    id=call_id,
                    name=name,
                    arguments_fragment=json.dumps(arguments),
                )
            yield FinishEvent("tool_calls", {})
            return
        if self.parent_calls == 3:
            self.check_result = json.loads(tool_messages["check-1"])
            assert json.loads(tool_messages["check-timeout"])["status"] == "running"
            self.list_result = json.loads(tool_messages["list-1"])
            self.management_seen.set()
            agent_id = self.check_result["agent_id"]
            yield ToolCallDelta(
                index=0,
                id="check-2",
                name="check_agent",
                arguments_fragment=json.dumps({"agent_id": agent_id, "timeout_seconds": 2.0}),
            )
            yield FinishEvent("tool_calls", {})
            return
        yield ContentDelta("parent result")
        yield FinishEvent("stop", {})


class _PrecedenceModel:
    def __init__(self) -> None:
        self.parent_calls = 0
        self.child_requests: dict[str, ModelRequest] = {}
        self.all_children_started = asyncio.Event()
        self.parent_tool_results: dict[str, str] = {}

    async def request(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"ordinary Runs must stream: {request}")

    async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        user_messages = [
            message["content"] for message in request.messages if message["role"] == "user"
        ]
        task = user_messages[-1]
        if task != "coordinate children":
            self.child_requests[task] = request
            if len(self.child_requests) == 3:
                self.all_children_started.set()
            yield ContentDelta(f"finished {task}")
            yield FinishEvent("stop", {})
            return

        self.parent_calls += 1
        if self.parent_calls == 1:
            calls = (
                (
                    "spawn-override",
                    {
                        "task": "override task",
                        "agent_type": "restricted",
                        "model": "spawn-model",
                    },
                ),
                (
                    "spawn-definition",
                    {"task": "definition task", "agent_type": "restricted"},
                ),
                ("spawn-default", {"task": "default task"}),
                (
                    "spawn-unavailable",
                    {"task": "must not start", "agent_type": "unavailable"},
                ),
                (
                    "spawn-empty-model",
                    {"task": "must not start either", "model": ""},
                ),
            )
            for index, (call_id, arguments) in enumerate(calls):
                yield ToolCallDelta(
                    index=index,
                    id=call_id,
                    name="spawn_agent",
                    arguments_fragment=json.dumps(arguments),
                )
            yield FinishEvent("tool_calls", {})
            return
        self.parent_tool_results = {
            message["tool_call_id"]: message["content"]
            for message in request.messages
            if message["role"] == "tool"
        }
        await self.all_children_started.wait()
        yield ContentDelta("coordinated")
        yield FinishEvent("stop", {})


class _CapacityModel:
    def __init__(self) -> None:
        self.parent_calls = 0
        self.release = {f"child-{index}": asyncio.Event() for index in range(5)}
        self.started = {f"child-{index}": asyncio.Event() for index in range(5)}
        self.capacity_seen = asyncio.Event()
        self.tool_results: dict[str, str] = {}
        self.cancelled_tasks: set[str] = set()

    async def request(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"ordinary Runs must stream: {request}")

    async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        user_messages = [
            message["content"] for message in request.messages if message["role"] == "user"
        ]
        task = user_messages[-1]
        if task != "coordinate capacity":
            self.started[task].set()
            try:
                await self.release[task].wait()
            except asyncio.CancelledError:
                self.cancelled_tasks.add(task)
                raise
            yield ContentDelta(f"finished {task}")
            yield FinishEvent("stop", {})
            return

        self.parent_calls += 1
        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in request.messages
            if message["role"] == "tool"
        }
        if self.parent_calls == 1:
            for index in range(5):
                yield ToolCallDelta(
                    index=index,
                    id=f"spawn-{index}",
                    name="spawn_agent",
                    arguments_fragment=json.dumps({"task": f"child-{index}"}),
                )
            yield FinishEvent("tool_calls", {})
            return
        if self.parent_calls == 2:
            self.tool_results.update(tool_messages)
            self.capacity_seen.set()
            first_agent_id = json.loads(tool_messages["spawn-0"])["agent_id"]
            yield ToolCallDelta(
                index=0,
                id="check-first",
                name="check_agent",
                arguments_fragment=json.dumps({"agent_id": first_agent_id, "timeout_seconds": 2.0}),
            )
            yield ToolCallDelta(
                index=1,
                id="spawn-replacement",
                name="spawn_agent",
                arguments_fragment=json.dumps({"task": "child-4"}),
            )
            yield FinishEvent("tool_calls", {})
            return
        self.tool_results.update(tool_messages)
        await self.started["child-4"].wait()
        yield ContentDelta("capacity coordinated")
        yield FinishEvent("stop", {})


class _SteeringModel:
    def __init__(self) -> None:
        self.parent_calls = 0
        self.child_calls = 0
        self.child_call_started = asyncio.Event()
        self.release_child_call = asyncio.Event()
        self.steering_queued = asyncio.Event()
        self.child_was_cancelled = False
        self.child_second_request: ModelRequest | None = None
        self.acknowledgements: list[dict[str, object]] = []

    async def request(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"ordinary Runs must stream: {request}")

    async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if request.model == "child-model":
            self.child_calls += 1
            if self.child_calls == 1:
                self.child_call_started.set()
                try:
                    await self.release_child_call.wait()
                except asyncio.CancelledError:
                    self.child_was_cancelled = True
                    raise
                yield ToolCallDelta(
                    index=0,
                    id="child-ls",
                    name="ls",
                    arguments_fragment="{}",
                )
                yield FinishEvent("tool_calls", {})
                return
            self.child_second_request = request
            yield ContentDelta("steered child result")
            yield FinishEvent("stop", {})
            return

        self.parent_calls += 1
        tool_messages = [message for message in request.messages if message["role"] == "tool"]
        if self.parent_calls == 1:
            yield ToolCallDelta(
                index=0,
                id="spawn-steered",
                name="spawn_agent",
                arguments_fragment=json.dumps({"task": "steered task", "model": "child-model"}),
            )
            yield FinishEvent("tool_calls", {})
            return
        agent_id = json.loads(
            next(
                message["content"]
                for message in tool_messages
                if message["tool_call_id"] == "spawn-steered"
            )
        )["agent_id"]
        if self.parent_calls == 2:
            await self.child_call_started.wait()
            for index, message in enumerate(("first steer", "second steer")):
                yield ToolCallDelta(
                    index=index,
                    id=f"steer-{index}",
                    name="steer_agent",
                    arguments_fragment=json.dumps({"agent_id": agent_id, "message": message}),
                )
            yield FinishEvent("tool_calls", {})
            return
        if self.parent_calls == 3:
            self.acknowledgements = [
                json.loads(message["content"])
                for message in tool_messages
                if str(message["tool_call_id"]).startswith("steer-")
            ]
            self.steering_queued.set()
            yield ToolCallDelta(
                index=0,
                id="check-steered",
                name="check_agent",
                arguments_fragment=json.dumps({"agent_id": agent_id, "timeout_seconds": 2.0}),
            )
            yield FinishEvent("tool_calls", {})
            return
        yield ContentDelta("steering coordinated")
        yield FinishEvent("stop", {})


class _RecursiveCloseModel:
    def __init__(self) -> None:
        self.parent_calls = 0
        self.child_calls = 0
        self.grandchild_started = asyncio.Event()
        self.never = asyncio.Event()
        self.child_cancelled = False
        self.grandchild_cancelled = False
        self.tool_results: dict[str, str] = {}

    async def request(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"ordinary Runs must stream: {request}")

    async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if request.model == "grandchild-model":
            self.grandchild_started.set()
            try:
                await self.never.wait()
            except asyncio.CancelledError:
                self.grandchild_cancelled = True
                raise
            yield ContentDelta("unreachable")
            return
        if request.model == "child-model":
            self.child_calls += 1
            if self.child_calls == 1:
                yield ToolCallDelta(
                    index=0,
                    id="spawn-grandchild",
                    name="spawn_agent",
                    arguments_fragment=json.dumps(
                        {"task": "grandchild task", "model": "grandchild-model"}
                    ),
                )
                yield FinishEvent("tool_calls", {})
                return
            try:
                await self.never.wait()
            except asyncio.CancelledError:
                self.child_cancelled = True
                raise
            yield ContentDelta("unreachable")
            return

        self.parent_calls += 1
        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in request.messages
            if message["role"] == "tool"
        }
        if self.parent_calls == 1:
            yield ToolCallDelta(
                index=0,
                id="spawn-child",
                name="spawn_agent",
                arguments_fragment=json.dumps({"task": "child task", "model": "child-model"}),
            )
            yield FinishEvent("tool_calls", {})
            return
        child_id = json.loads(tool_messages["spawn-child"])["agent_id"]
        if self.parent_calls == 2:
            await self.grandchild_started.wait()
            calls = (
                ("close-first", "close_agent", {"agent_id": child_id}),
                ("close-again", "close_agent", {"agent_id": child_id}),
                (
                    "steer-terminal",
                    "steer_agent",
                    {"agent_id": child_id, "message": "too late"},
                ),
                ("check-terminal", "check_agent", {"agent_id": child_id}),
                ("check-unknown", "check_agent", {"agent_id": "agent-missing"}),
            )
            for index, (call_id, name, arguments) in enumerate(calls):
                yield ToolCallDelta(
                    index=index,
                    id=call_id,
                    name=name,
                    arguments_fragment=json.dumps(arguments),
                )
            yield FinishEvent("tool_calls", {})
            return
        self.tool_results.update(tool_messages)
        yield ContentDelta("closed recursively")
        yield FinishEvent("stop", {})


class _CompletionCloseRaceModel:
    def __init__(self) -> None:
        self.parent_calls = 0
        self.child_started = asyncio.Event()
        self.release_child = asyncio.Event()
        self.agent_id: str | None = None
        self.close_status: str | None = None
        self.check_status: str | None = None

    async def request(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"ordinary Runs must stream: {request}")

    async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        task = next(message["content"] for message in request.messages if message["role"] == "user")
        if task == "race child":
            self.child_started.set()
            await self.release_child.wait()
            yield ContentDelta("child completed")
            yield FinishEvent("stop", {})
            return

        self.parent_calls += 1
        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in request.messages
            if message["role"] == "tool"
        }
        if self.parent_calls == 1:
            yield ToolCallDelta(
                index=0,
                id="spawn-race",
                name="spawn_agent",
                arguments_fragment=json.dumps({"task": "race child"}),
            )
            yield FinishEvent("tool_calls", {})
            return
        if self.parent_calls == 2:
            await self.child_started.wait()
            self.agent_id = json.loads(tool_messages["spawn-race"])["agent_id"]
            yield ToolCallDelta(
                index=0,
                id="release-race",
                name="release_race_child",
                arguments_fragment="{}",
            )
            yield ToolCallDelta(
                index=1,
                id="close-race",
                name="close_agent",
                arguments_fragment=json.dumps({"agent_id": self.agent_id}),
            )
            yield FinishEvent("tool_calls", {})
            return
        if self.parent_calls == 3:
            self.close_status = json.loads(tool_messages["close-race"])["status"]
            yield ToolCallDelta(
                index=0,
                id="check-race",
                name="check_agent",
                arguments_fragment=json.dumps({"agent_id": self.agent_id}),
            )
            yield FinishEvent("tool_calls", {})
            return
        self.check_status = json.loads(tool_messages["check-race"])["status"]
        yield ContentDelta("race observed")
        yield FinishEvent("stop", {})


class _DepthModel:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.depth_rejection: str | None = None

    async def request(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"ordinary Runs must stream: {request}")

    async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        task = next(message["content"] for message in request.messages if message["role"] == "user")
        depth = int(task.removeprefix("depth-"))
        self.calls[task] = self.calls.get(task, 0) + 1
        call_index = self.calls[task]
        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in request.messages
            if message["role"] == "tool"
        }
        if call_index == 1:
            yield ToolCallDelta(
                index=0,
                id=f"spawn-depth-{depth}",
                name="spawn_agent",
                arguments_fragment=json.dumps({"task": f"depth-{depth + 1}"}),
            )
            yield FinishEvent("tool_calls", {})
            return
        spawn_result = tool_messages[f"spawn-depth-{depth}"]
        if depth == 3:
            self.depth_rejection = spawn_result
            yield ContentDelta("depth limit observed")
            yield FinishEvent("stop", {})
            return
        if call_index == 2:
            agent_id = json.loads(spawn_result)["agent_id"]
            yield ToolCallDelta(
                index=0,
                id=f"check-depth-{depth}",
                name="check_agent",
                arguments_fragment=json.dumps({"agent_id": agent_id, "timeout_seconds": 2.0}),
            )
            yield FinishEvent("tool_calls", {})
            return
        yield ContentDelta(f"depth-{depth} complete")
        yield FinishEvent("stop", {})


class _TerminalCleanupModel:
    def __init__(self, terminal: str) -> None:
        self.terminal = terminal
        self.parent_calls = 0
        self.child_started = asyncio.Event()
        self.parent_blocked = asyncio.Event()
        self.never = asyncio.Event()
        self.child_cancelled = False

    async def request(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"ordinary Runs must stream: {request}")

    async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if request.model == "child-model":
            self.child_started.set()
            try:
                await self.never.wait()
            except asyncio.CancelledError:
                self.child_cancelled = True
                raise
            yield ContentDelta("unreachable")
            return

        self.parent_calls += 1
        if self.parent_calls == 1:
            yield ToolCallDelta(
                index=0,
                id="spawn-terminal-child",
                name="spawn_agent",
                arguments_fragment=json.dumps({"task": "blocked child", "model": "child-model"}),
            )
            yield ToolCallDelta(
                index=1,
                id="sync-terminal-child",
                name="sync_terminal_child",
                arguments_fragment="{}",
            )
            yield FinishEvent("tool_calls", {})
            return
        if self.terminal == "failed":
            raise RuntimeError("parent model failed")
        self.parent_blocked.set()
        await self.never.wait()
        yield ContentDelta("unreachable")


class _ScopedRunsModel:
    def __init__(self) -> None:
        self.calls = {"a": 0, "b": 0}
        self.agent_ids: dict[str, str] = {}
        self.both_spawned = asyncio.Event()
        self.never = asyncio.Event()
        self.results: dict[str, dict[str, str]] = {}

    async def request(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"ordinary Runs must stream: {request}")

    async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        assert request.model is not None
        if request.model.startswith("child-"):
            await self.never.wait()
            yield ContentDelta("unreachable")
            return
        label = request.model.removeprefix("root-")
        self.calls[label] += 1
        call_index = self.calls[label]
        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in request.messages
            if message["role"] == "tool"
        }
        if call_index == 1:
            yield ToolCallDelta(
                index=0,
                id=f"spawn-{label}",
                name="spawn_agent",
                arguments_fragment=json.dumps(
                    {"task": f"child {label}", "model": f"child-{label}"}
                ),
            )
            yield FinishEvent("tool_calls", {})
            return
        if call_index == 2:
            own_id = json.loads(tool_messages[f"spawn-{label}"])["agent_id"]
            self.agent_ids[label] = own_id
            if len(self.agent_ids) == 2:
                self.both_spawned.set()
            await self.both_spawned.wait()
            other_label = "b" if label == "a" else "a"
            other_id = self.agent_ids[other_label]
            calls = (
                (f"list-{label}", "list_agents", {}),
                (f"check-own-{label}", "check_agent", {"agent_id": own_id}),
                (f"check-other-{label}", "check_agent", {"agent_id": other_id}),
                (
                    f"steer-other-{label}",
                    "steer_agent",
                    {"agent_id": other_id, "message": "inaccessible"},
                ),
                (f"close-other-{label}", "close_agent", {"agent_id": other_id}),
                (f"close-own-{label}", "close_agent", {"agent_id": own_id}),
            )
            for index, (call_id, name, arguments) in enumerate(calls):
                yield ToolCallDelta(
                    index=index,
                    id=call_id,
                    name=name,
                    arguments_fragment=json.dumps(arguments),
                )
            yield FinishEvent("tool_calls", {})
            return
        self.results[label] = tool_messages
        yield ContentDelta(f"root {label} done")
        yield FinishEvent("stop", {})


class _SharedPolicyModel:
    def __init__(self) -> None:
        self.parent_calls = 0
        self.child_calls = 0
        self.child_bash_result: str | None = None

    async def request(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"ordinary Runs must stream: {request}")

    async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if request.model == "child-model":
            self.child_calls += 1
            if self.child_calls == 1:
                yield ToolCallDelta(
                    index=0,
                    id="child-bash",
                    name="bash",
                    arguments_fragment=json.dumps({"command": "echo must-not-run"}),
                )
                yield FinishEvent("tool_calls", {})
                return
            self.child_bash_result = next(
                message["content"]
                for message in request.messages
                if message.get("tool_call_id") == "child-bash"
            )
            yield ContentDelta("child respected policy")
            yield FinishEvent("stop", {})
            return

        self.parent_calls += 1
        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in request.messages
            if message["role"] == "tool"
        }
        if self.parent_calls == 1:
            yield ToolCallDelta(
                index=0,
                id="spawn-policy-child",
                name="spawn_agent",
                arguments_fragment=json.dumps(
                    {"task": "test shared policy", "model": "child-model"}
                ),
            )
            yield FinishEvent("tool_calls", {})
            return
        if self.parent_calls == 2:
            agent_id = json.loads(tool_messages["spawn-policy-child"])["agent_id"]
            yield ToolCallDelta(
                index=0,
                id="check-policy-child",
                name="check_agent",
                arguments_fragment=json.dumps({"agent_id": agent_id, "timeout_seconds": 2.0}),
            )
            yield FinishEvent("tool_calls", {})
            return
        yield ContentDelta("policy coordinated")
        yield FinishEvent("stop", {})


class _ChildFailureModel:
    def __init__(self) -> None:
        self.parent_calls = 0
        self.max_child_calls = 0
        self.statuses: dict[str, dict[str, object]] = {}

    async def request(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"ordinary Runs must stream: {request}")

    async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if request.model == "child-fail":
            raise RuntimeError("child model failed")
            yield ContentDelta("unreachable")
        if request.model == "child-max":
            self.max_child_calls += 1
            yield ToolCallDelta(
                index=0,
                id=f"max-ls-{self.max_child_calls}",
                name="ls",
                arguments_fragment="{}",
            )
            yield FinishEvent("tool_calls", {})
            return

        self.parent_calls += 1
        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in request.messages
            if message["role"] == "tool"
        }
        if self.parent_calls == 1:
            for index, (call_id, task, model) in enumerate(
                (
                    ("spawn-failing-child", "failing child", "child-fail"),
                    ("spawn-max-child", "max child", "child-max"),
                )
            ):
                yield ToolCallDelta(
                    index=index,
                    id=call_id,
                    name="spawn_agent",
                    arguments_fragment=json.dumps({"task": task, "model": model}),
                )
            yield FinishEvent("tool_calls", {})
            return
        if self.parent_calls == 2:
            failing_id = json.loads(tool_messages["spawn-failing-child"])["agent_id"]
            max_id = json.loads(tool_messages["spawn-max-child"])["agent_id"]
            for index, (call_id, agent_id) in enumerate(
                (
                    ("check-failing-child", failing_id),
                    ("check-max-child", max_id),
                )
            ):
                yield ToolCallDelta(
                    index=index,
                    id=call_id,
                    name="check_agent",
                    arguments_fragment=json.dumps({"agent_id": agent_id, "timeout_seconds": 2.0}),
                )
            yield FinishEvent("tool_calls", {})
            return
        self.statuses = {
            "failing": json.loads(tool_messages["check-failing-child"]),
            "max": json.loads(tool_messages["check-max-child"]),
        }
        yield ContentDelta("failures observed")
        yield FinishEvent("stop", {})


async def test_parent_run_delegates_to_an_isolated_durable_subagent(tmp_path: Path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    _write_agent_definition(cwd / ".phi" / "agents" / "specialist.md")
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base instructions.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    parent = await create_session(storage, model="parent-model")
    model = _DelegationModel()

    parent_task = asyncio.create_task(
        send_message(
            parent,
            "parent secret coordination",
            storage=storage,
            settings=Settings(),
            model=model,
            model_info=None,
            tools=resources.tools,
            dispatcher=resources.dispatcher,
            stable_instructions=resources.stable_instructions,
            max_steps=4,
            lifecycle=resources.agents,
        )
    )
    await model.child_started.wait()
    await model.management_seen.wait()

    assert not parent_task.done()
    assert model.check_result is not None
    assert model.check_result["status"] == "running"
    assert model.check_result["result"] is None
    assert model.list_result == [
        {
            "agent_id": model.check_result["agent_id"],
            "result": None,
            "status": "running",
            "task": "isolated task",
        }
    ]

    model.release_child.set()
    parent_handle, parent_result = await parent_task

    assert parent_result.output == "parent result"
    sessions = await list_sessions(storage)
    child_metadata = next(metadata for metadata in sessions if metadata.origin == "subagent")
    assert child_metadata.parent_session_id == parent_handle.session_id
    assert child_metadata.fork_point_entry_id is None
    assert child_metadata.model == "child-model"
    child_handle = await resume_session(storage, child_metadata.id)
    child = await materialize_conversation(storage, child_handle)
    assert len(child.entries) == 2
    assert isinstance(child.entries[0], UserMessageEntry)
    assert child.entries[0].content == "isolated task"
    assert isinstance(child.entries[1], AssistantMessageEntry)
    assert child.entries[1].content == "child result"
    assert model.child_requests[0].messages[0]["content"] == (
        "--- BEGIN PHI BASE INSTRUCTIONS ---\n"
        "Phi base instructions.\n"
        "--- END PHI BASE INSTRUCTIONS ---\n\n"
        "--- BEGIN AGENT DEFINITION ---\n"
        "Follow the specialist instructions.\n"
        "--- END AGENT DEFINITION ---"
    )
    assert "parent secret coordination" not in str(model.child_requests[0].messages)
    assert storage.trace_path(child_metadata.id).read_text(encoding="utf-8")
    await resources.close()


async def test_agent_tool_rejections_are_typed_and_approval_happens_before_creation(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    definition = cwd / ".phi" / "agents" / "disabled.md"
    definition.parent.mkdir(parents=True)
    definition.write_text(
        "---\n"
        "name: disabled\n"
        "description: Trusted route only.\n"
        "disable-model-invocation: true\n"
        "---\n"
        "Do not expose this definition.\n",
        encoding="utf-8",
    )
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    context = DelegationContext(
        root_owner_run_id="owner",
        current_run_id="run",
        current_session_id="session",
        current_agent_id=None,
        depth=0,
    )
    dispatcher = resources.dispatcher.with_trusted_values(
        {"runtime": resources.agents, "context": context}
    )

    disabled = await dispatcher.dispatch(
        ToolCall(
            id="disabled-call",
            name="spawn_agent",
            arguments={"task": "work", "agent_type": "disabled"},
        )
    )
    unknown = await dispatcher.dispatch(
        ToolCall(
            id="unknown-call",
            name="spawn_agent",
            arguments={"task": "work", "agent_type": "missing"},
        )
    )
    invalid_timeout = await dispatcher.dispatch(
        ToolCall(
            id="timeout-call",
            name="check_agent",
            arguments={"agent_id": "agent", "timeout_seconds": float("inf")},
        )
    )
    extra_argument = await dispatcher.dispatch(
        ToolCall(
            id="extra-call",
            name="list_agents",
            arguments={"untrusted": True},
        )
    )

    assert disabled.error == "model_invocation_disabled: disabled"
    assert unknown.error == "unknown_agent_type: missing"
    assert invalid_timeout.error is not None
    assert invalid_timeout.error.startswith("invalid_arguments:")
    assert extra_argument.error is not None
    assert extra_argument.error.startswith("invalid_arguments:")
    await resources.close()

    denied_resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )
    denied = await denied_resources.dispatcher.dispatch(
        ToolCall(
            id="denied-call",
            name="spawn_agent",
            arguments={"task": "must not create anything"},
        )
    )
    assert denied.error == "approval_denied: spawn_agent"
    await denied_resources.close()


async def test_child_model_and_tool_authority_follow_spawn_definition_parent_precedence(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    definitions = cwd / ".phi" / "agents"
    definitions.mkdir(parents=True)
    (definitions / "restricted.md").write_text(
        "---\n"
        "name: restricted\n"
        "description: Use only the list Tool.\n"
        "tools: [ls]\n"
        "model: definition-model\n"
        "---\n"
        "Use the restricted specialist.\n",
        encoding="utf-8",
    )
    (definitions / "unavailable.md").write_text(
        "---\n"
        "name: unavailable\n"
        "description: Requests an unavailable Tool.\n"
        "tools: [missing-tool]\n"
        "---\n"
        "This definition must be rejected before Session creation.\n",
        encoding="utf-8",
    )
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    parent = await create_session(storage, model="parent-model")
    model = _PrecedenceModel()

    _, result = await send_message(
        parent,
        "coordinate children",
        storage=storage,
        settings=Settings(),
        model=model,
        model_info=None,
        tools=resources.tools,
        dispatcher=resources.dispatcher,
        stable_instructions=resources.stable_instructions,
        max_steps=2,
        lifecycle=resources.agents,
    )

    assert result.output == "coordinated"
    assert model.child_requests["override task"].model == "spawn-model"
    assert model.child_requests["definition task"].model == "definition-model"
    assert model.child_requests["default task"].model == "parent-model"
    for task in ("override task", "definition task"):
        assert [spec["function"]["name"] for spec in model.child_requests[task].tools] == ["ls"]
    assert "spawn_agent" in {
        spec["function"]["name"] for spec in model.child_requests["default task"].tools
    }
    assert model.parent_tool_results["spawn-unavailable"] == (
        "unavailable_agent_tool: missing-tool"
    )
    assert model.parent_tool_results["spawn-empty-model"].startswith("invalid_arguments:")
    assert len(await list_sessions(storage)) == 4
    await resources.close()


async def test_capacity_is_atomic_releases_after_completion_and_parent_cleanup_awaits_children(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    parent = await create_session(storage, model="model")
    model = _CapacityModel()
    parent_task = asyncio.create_task(
        send_message(
            parent,
            "coordinate capacity",
            storage=storage,
            settings=Settings(),
            model=model,
            model_info=None,
            tools=resources.tools,
            dispatcher=resources.dispatcher,
            stable_instructions=resources.stable_instructions,
            max_steps=3,
            lifecycle=resources.agents,
        )
    )
    await model.capacity_seen.wait()

    assert model.tool_results["spawn-4"] == (
        "agent_capacity_exceeded: maximum running Subagents is 4"
    )
    model.release["child-0"].set()
    _, result = await parent_task

    assert result.output == "capacity coordinated"
    assert json.loads(model.tool_results["check-first"])["status"] == "completed"
    assert "agent_id" in json.loads(model.tool_results["spawn-replacement"])
    assert model.cancelled_tasks == {"child-1", "child-2", "child-3", "child-4"}
    assert len(await list_sessions(storage)) == 6
    await resources.close()


async def test_steering_is_non_destructive_ordered_once_and_composes_with_message_injection(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    parent = await create_session(storage, model="parent-model")
    model = _SteeringModel()

    async def existing_injection() -> list[str]:
        return ["existing injection"]

    parent_task = asyncio.create_task(
        send_message(
            parent,
            "coordinate steering",
            storage=storage,
            settings=Settings(),
            model=model,
            model_info=None,
            tools=resources.tools,
            dispatcher=resources.dispatcher,
            stable_instructions=resources.stable_instructions,
            max_steps=4,
            hooks=Hooks(inject_messages=existing_injection),
            lifecycle=resources.agents,
        )
    )
    await model.child_call_started.wait()
    await model.steering_queued.wait()

    assert not model.child_was_cancelled
    assert not parent_task.done()
    model.release_child_call.set()
    _, result = await parent_task

    assert result.output == "steering coordinated"
    assert model.acknowledgements and all(
        acknowledgement["queued"] is True for acknowledgement in model.acknowledgements
    )
    assert model.child_second_request is not None
    child_user_messages = [
        message["content"]
        for message in model.child_second_request.messages
        if message["role"] == "user"
    ]
    assert child_user_messages[-3:] == [
        "existing injection",
        "first steer",
        "second steer",
    ]
    assert child_user_messages.count("first steer") == 1
    assert child_user_messages.count("second steer") == 1
    assert not model.child_was_cancelled
    await resources.close()


async def test_close_agent_recursively_awaits_descendants_and_is_idempotent(tmp_path: Path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    parent = await create_session(storage, model="parent-model")
    model = _RecursiveCloseModel()

    _, result = await send_message(
        parent,
        "coordinate close",
        storage=storage,
        settings=Settings(),
        model=model,
        model_info=None,
        tools=resources.tools,
        dispatcher=resources.dispatcher,
        stable_instructions=resources.stable_instructions,
        max_steps=3,
        lifecycle=resources.agents,
    )

    assert result.output == "closed recursively"
    assert model.child_cancelled
    assert model.grandchild_cancelled
    assert json.loads(model.tool_results["close-first"])["status"] == "cancelled"
    assert json.loads(model.tool_results["close-again"])["status"] == "cancelled"
    assert model.tool_results["steer-terminal"].startswith("agent_not_running:")
    assert json.loads(model.tool_results["check-terminal"])["status"] == "cancelled"
    assert model.tool_results["check-unknown"] == "unknown_agent: agent-missing"
    assert len(await list_sessions(storage)) == 3
    await resources.close()


async def test_simultaneous_completion_and_close_has_one_terminal_transition(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    parent = await create_session(storage, model="parent-model")
    model = _CompletionCloseRaceModel()

    @tool(name="release_race_child", description="Release the racing child completion.")
    async def release_race_child() -> str:
        model.release_child.set()
        await asyncio.sleep(0)
        return "released"

    resources.tools.register(release_race_child)
    _, result = await send_message(
        parent,
        "coordinate completion-close race",
        storage=storage,
        settings=Settings(),
        model=model,
        model_info=None,
        tools=resources.tools,
        dispatcher=resources.dispatcher,
        stable_instructions=resources.stable_instructions,
        max_steps=4,
        lifecycle=resources.agents,
    )

    assert result.output == "race observed"
    assert model.close_status in {"completed", "cancelled"}
    assert model.check_status == model.close_status
    await resources.close()


async def test_delegation_depth_allows_three_subagent_levels_and_rejects_the_next(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    root = await create_session(storage, model="model")
    model = _DepthModel()

    _, result = await send_message(
        root,
        "depth-0",
        storage=storage,
        settings=Settings(),
        model=model,
        model_info=None,
        tools=resources.tools,
        dispatcher=resources.dispatcher,
        stable_instructions=resources.stable_instructions,
        max_steps=3,
        lifecycle=resources.agents,
    )

    assert result.output == "depth-0 complete"
    assert model.depth_rejection == "delegation_depth_exceeded: maximum depth is 3"
    assert len(await list_sessions(storage)) == 4
    await resources.close()


@pytest.mark.parametrize(
    ("terminal", "max_steps", "expected_status"),
    (
        ("max_steps", 1, RunStatus.MAX_STEPS),
        ("failed", 2, RunStatus.FAILED),
        ("cancelled", 2, RunStatus.CANCELLED),
    ),
)
async def test_unfinished_children_are_awaited_before_every_parent_terminal_status(
    tmp_path: Path,
    terminal: str,
    max_steps: int,
    expected_status: RunStatus,
) -> None:
    cwd = tmp_path / terminal
    cwd.mkdir()
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / f"sessions-{terminal}")
    parent = await create_session(storage, model="parent-model")
    model = _TerminalCleanupModel(terminal)

    @tool(name="sync_terminal_child", description="Wait until the test child Model starts.")
    async def sync_terminal_child() -> str:
        await model.child_started.wait()
        return "started"

    resources.tools.register(sync_terminal_child)
    terminal_cleanup_observations: list[bool] = []

    def observe_terminal(event: object) -> None:
        if isinstance(event, RunFinished):
            terminal_cleanup_observations.append(model.child_cancelled)

    send_task = asyncio.create_task(
        send_message(
            parent,
            f"parent ends as {terminal}",
            storage=storage,
            settings=Settings(),
            model=model,
            model_info=None,
            tools=resources.tools,
            dispatcher=resources.dispatcher,
            stable_instructions=resources.stable_instructions,
            max_steps=max_steps,
            events=EventBus([observe_terminal]),
            lifecycle=resources.agents,
        )
    )
    if terminal == "cancelled":
        await model.parent_blocked.wait()
        send_task.cancel()
    _, result = await send_task

    assert result.status is expected_status
    assert model.child_cancelled
    if terminal != "cancelled":
        assert terminal_cleanup_observations == [True]
    child_metadata = next(
        metadata for metadata in await list_sessions(storage) if metadata.origin == "subagent"
    )
    assert storage.trace_path(child_metadata.id).read_text(encoding="utf-8")
    await resources.close()


async def test_scheduling_failure_rolls_back_the_empty_child_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    parent = await create_session(storage, model="model")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="spawn-fails",
                        name="spawn_agent",
                        arguments={"task": "must roll back"},
                    )
                ]
            ),
            ModelResponse(content="recovered"),
        ]
    )

    def fail_scheduling(coroutine: Coroutine[object, object, object]) -> None:
        coroutine.close()
        raise RuntimeError("task scheduling failed")

    monkeypatch.setattr(asyncio, "create_task", fail_scheduling)
    _, result = await send_message(
        parent,
        "trigger scheduling failure",
        storage=storage,
        settings=Settings(),
        model=model,
        model_info=None,
        tools=resources.tools,
        dispatcher=resources.dispatcher,
        stable_instructions=resources.stable_instructions,
        max_steps=2,
        lifecycle=resources.agents,
    )

    assert result.output == "recovered"
    assert result.steps[0].tool_results[0].error == (
        "handler_error: RuntimeError: task scheduling failed"
    )
    assert [metadata.origin for metadata in await list_sessions(storage)] == ["new"]
    await resources.close()


async def test_session_creation_failure_removes_partial_child_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    parent = await create_session(storage, model="model")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="spawn-fails",
                        name="spawn_agent",
                        arguments={"task": "must roll back"},
                    )
                ]
            ),
            ModelResponse(content="recovered"),
        ]
    )

    original_metadata_write = storage._atomic_write_metadata

    def fail_after_journal_creation(envelope: SessionMetadataEnvelope) -> None:
        if envelope.metadata.origin == "subagent":
            raise OSError("metadata write failed")
        original_metadata_write(envelope)

    monkeypatch.setattr(storage, "_atomic_write_metadata", fail_after_journal_creation)
    _, result = await send_message(
        parent,
        "trigger creation failure",
        storage=storage,
        settings=Settings(),
        model=model,
        model_info=None,
        tools=resources.tools,
        dispatcher=resources.dispatcher,
        stable_instructions=resources.stable_instructions,
        max_steps=2,
        lifecycle=resources.agents,
    )

    assert result.output == "recovered"
    assert result.steps[0].tool_results[0].error == (
        "handler_error: OSError: metadata write failed"
    )
    assert [metadata.origin for metadata in await list_sessions(storage)] == ["new"]
    assert sorted(path.name for path in storage.root.iterdir()) == [
        f"{parent.session_id}.jsonl",
        f"{parent.session_id}.metadata.json",
        f"{parent.session_id}.trace.jsonl",
    ]
    await resources.close()


async def test_shutdown_during_spawn_waits_for_rollback_without_an_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    parent = await create_session(storage, model="model")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="spawn-shutdown",
                        name="spawn_agent",
                        arguments={"task": "race shutdown"},
                    )
                ]
            ),
            ModelResponse(content="shutdown observed"),
        ]
    )
    creation_started = threading.Event()
    release_creation = threading.Event()
    original_create = storage._create_sync

    def block_child_creation(envelope: SessionMetadataEnvelope) -> None:
        creation_started.set()
        release_creation.wait()
        original_create(envelope)

    monkeypatch.setattr(storage, "_create_sync", block_child_creation)
    send_task = asyncio.create_task(
        send_message(
            parent,
            "spawn while shutting down",
            storage=storage,
            settings=Settings(),
            model=model,
            model_info=None,
            tools=resources.tools,
            dispatcher=resources.dispatcher,
            stable_instructions=resources.stable_instructions,
            max_steps=2,
            lifecycle=resources.agents,
        )
    )
    await asyncio.to_thread(creation_started.wait)
    close_task = asyncio.create_task(resources.close())
    await asyncio.sleep(0)
    assert not close_task.done()

    release_creation.set()
    await close_task
    _, result = await send_task

    assert result.output == "shutdown observed"
    assert result.steps[0].tool_results[0].error == (
        "agent_runtime_closed: runtime is shutting down"
    )
    assert [metadata.origin for metadata in await list_sessions(storage)] == ["new"]


async def test_cancellation_during_spawn_activation_rolls_back_every_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    parent = await create_session(storage, model="model")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="spawn-cancelled",
                        name="spawn_agent",
                        arguments={"task": "cancel activation"},
                    )
                ]
            )
        ]
    )
    activation_started = asyncio.Event()
    never_activate = asyncio.Event()

    async def block_activation(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        activation_started.set()
        await never_activate.wait()
        return True

    monkeypatch.setattr(resources.agents._registry, "activate", block_activation)
    send_task = asyncio.create_task(
        send_message(
            parent,
            "cancel while activating",
            storage=storage,
            settings=Settings(),
            model=model,
            model_info=None,
            tools=resources.tools,
            dispatcher=resources.dispatcher,
            stable_instructions=resources.stable_instructions,
            max_steps=1,
            lifecycle=resources.agents,
        )
    )
    await activation_started.wait()
    send_task.cancel()
    _, result = await send_task

    assert result.status is RunStatus.CANCELLED
    assert [metadata.origin for metadata in await list_sessions(storage)] == ["new"]
    await resources.close()


async def test_cwd_replacement_cancels_subagents_before_returning_new_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    bootstrap = CwdRuntimeBootstrap(
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    resources = await bootstrap.load(first_cwd)
    storage = SessionStorage(tmp_path / "sessions")
    parent = await create_session(storage, model="parent-model")
    model = _TerminalCleanupModel("cancelled")
    transport_close_order: list[str] = []
    original_mcp_close = resources.mcp.close

    async def close_transport_after_agents() -> None:
        assert model.child_cancelled
        transport_close_order.append("mcp")
        await original_mcp_close()

    monkeypatch.setattr(resources.mcp, "close", close_transport_after_agents)

    @tool(name="sync_terminal_child", description="Wait until the test child Model starts.")
    async def sync_terminal_child() -> str:
        await model.child_started.wait()
        return "started"

    resources.tools.register(sync_terminal_child)
    send_task = asyncio.create_task(
        send_message(
            parent,
            "replace cwd while child runs",
            storage=storage,
            settings=Settings(),
            model=model,
            model_info=None,
            tools=resources.tools,
            dispatcher=resources.dispatcher,
            stable_instructions=resources.stable_instructions,
            max_steps=2,
            lifecycle=resources.agents,
        )
    )
    await model.parent_blocked.wait()

    replacement = await bootstrap.load(second_cwd)

    assert replacement.cwd == second_cwd.resolve()
    assert model.child_cancelled
    assert transport_close_order == ["mcp"]
    send_task.cancel()
    _, result = await send_task
    assert result.status is RunStatus.CANCELLED
    await bootstrap.close()


async def test_management_tools_cannot_access_children_owned_by_another_run(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    parent_a = await create_session(storage, model="root-a")
    parent_b = await create_session(storage, model="root-b")
    model = _ScopedRunsModel()

    async def run_parent(parent: SessionHandle, label: str) -> None:
        _, result = await send_message(
            parent,
            f"coordinate {label}",
            storage=storage,
            settings=Settings(),
            model=model,
            model_info=None,
            tools=resources.tools,
            dispatcher=resources.dispatcher,
            stable_instructions=resources.stable_instructions,
            max_steps=3,
            lifecycle=resources.agents,
        )
        assert result.output == f"root {label} done"

    await asyncio.gather(run_parent(parent_a, "a"), run_parent(parent_b, "b"))

    for label in ("a", "b"):
        own_id = model.agent_ids[label]
        other_label = "b" if label == "a" else "a"
        other_id = model.agent_ids[other_label]
        listed = json.loads(model.results[label][f"list-{label}"])
        assert [agent["agent_id"] for agent in listed] == [own_id]
        assert json.loads(model.results[label][f"check-own-{label}"])["status"] == "running"
        assert model.results[label][f"check-other-{label}"] == f"unknown_agent: {other_id}"
        assert model.results[label][f"steer-other-{label}"] == f"unknown_agent: {other_id}"
        assert model.results[label][f"close-other-{label}"] == f"unknown_agent: {other_id}"
        assert json.loads(model.results[label][f"close-own-{label}"])["status"] == "cancelled"
    await resources.close()


async def test_child_tool_calls_reuse_the_parent_approval_policy_instance(tmp_path: Path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    policy = _SelectiveApprovalPolicy()
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=policy,
    )
    storage = SessionStorage(tmp_path / "sessions")
    parent = await create_session(storage, model="parent-model")
    model = _SharedPolicyModel()

    _, result = await send_message(
        parent,
        "coordinate policy",
        storage=storage,
        settings=Settings(),
        model=model,
        model_info=None,
        tools=resources.tools,
        dispatcher=resources.dispatcher,
        stable_instructions=resources.stable_instructions,
        max_steps=3,
        lifecycle=resources.agents,
    )

    assert result.output == "policy coordinated"
    assert model.child_bash_result == "approval_denied: bash"
    assert policy.calls == ["spawn_agent", "check_agent", "bash"]
    await resources.close()


async def test_child_model_failure_and_max_steps_map_to_safe_failed_statuses(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    parent = await create_session(storage, model="parent-model")
    model = _ChildFailureModel()

    _, result = await send_message(
        parent,
        "observe child failures",
        storage=storage,
        settings=Settings(),
        model=model,
        model_info=None,
        tools=resources.tools,
        dispatcher=resources.dispatcher,
        stable_instructions=resources.stable_instructions,
        max_steps=3,
        lifecycle=resources.agents,
    )

    assert result.output == "failures observed"
    assert model.statuses["failing"]["status"] == "failed"
    assert model.statuses["failing"]["result"] == "run_failed: RuntimeError"
    assert model.statuses["max"]["status"] == "failed"
    assert model.statuses["max"]["result"] == "max_steps_exhausted"
    await resources.close()
