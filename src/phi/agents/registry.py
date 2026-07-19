from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from phi.agents.definition import DEFAULT_AGENT_DEFINITION, AgentDefinition
from phi.harness import Hooks, RunStatus
from phi.sessions import RunInvocation, create_session, send_message
from phi.tools import ToolFailure, ToolRegistry

MAX_DELEGATION_DEPTH = 3
MAX_RUNNING_SUBAGENTS = 4
MAX_CHECK_TIMEOUT_SECONDS = 30.0


class AgentStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class DelegationContext:
    """Trusted identity and lineage bound to one exact invoking Run."""

    root_owner_run_id: str
    current_run_id: str
    current_session_id: str
    current_agent_id: str | None
    depth: int


@dataclass(frozen=True)
class _AgentLineage:
    root_owner_run_id: str
    current_agent_id: str
    depth: int


@dataclass(frozen=True)
class AgentSnapshot:
    agent_id: str
    task: str
    status: AgentStatus
    result: str | None
    session_id: str

    def public(self, *, include_task: bool = False) -> dict[str, object]:
        value: dict[str, object] = {
            "agent_id": self.agent_id,
            "status": self.status.value,
            "result": self.result,
        }
        if include_task:
            value["task"] = self.task
        return value


@dataclass(frozen=True)
class _Reservation:
    token: str
    agent_id: str
    sequence: int


@dataclass
class _SpawnedAgent:
    agent_id: str
    sequence: int
    owner_run_id: str
    parent_agent_id: str | None
    task: str
    session_id: str
    task_handle: asyncio.Task[None]
    status: AgentStatus = AgentStatus.RUNNING
    result: str | None = None
    completion_event: asyncio.Event = field(default_factory=asyncio.Event)
    steering: deque[str] = field(default_factory=deque)
    cancellation_requested: bool = False

    def snapshot(self) -> AgentSnapshot:
        return AgentSnapshot(
            agent_id=self.agent_id,
            task=self.task,
            status=self.status,
            result=self.result,
            session_id=self.session_id,
        )


class _AgentCapacityError(Exception):
    pass


class _AgentRegistryClosedError(Exception):
    pass


class AgentRegistry:
    """Race-safe cwd-scoped index for live and terminal Subagents."""

    def __init__(self) -> None:
        self._agents: dict[str, _SpawnedAgent] = {}
        self._reservations: dict[str, _Reservation] = {}
        self._next_sequence = 0
        self._closed = False
        self._lock = asyncio.Lock()

    async def reserve(self) -> _Reservation:
        async with self._lock:
            if self._closed:
                raise _AgentRegistryClosedError
            running = sum(agent.status is AgentStatus.RUNNING for agent in self._agents.values())
            if running + len(self._reservations) >= MAX_RUNNING_SUBAGENTS:
                raise _AgentCapacityError
            reservation = _Reservation(
                token=str(uuid4()),
                agent_id=f"agent-{uuid4()}",
                sequence=self._next_sequence,
            )
            self._next_sequence += 1
            self._reservations[reservation.token] = reservation
            return reservation

    async def release(self, reservation: _Reservation) -> None:
        async with self._lock:
            self._reservations.pop(reservation.token, None)

    async def activate(
        self,
        reservation: _Reservation,
        *,
        context: DelegationContext,
        task: str,
        session_id: str,
        task_handle: asyncio.Task[None],
    ) -> bool:
        async with self._lock:
            current = self._reservations.pop(reservation.token, None)
            if self._closed or current != reservation:
                return False
            self._agents[reservation.agent_id] = _SpawnedAgent(
                agent_id=reservation.agent_id,
                sequence=reservation.sequence,
                owner_run_id=context.root_owner_run_id,
                parent_agent_id=context.current_agent_id,
                task=task,
                session_id=session_id,
                task_handle=task_handle,
            )
            return True

    async def abort_spawn(
        self,
        reservation: _Reservation,
        task_handle: asyncio.Task[None],
    ) -> None:
        """Release either phase of a spawn and await its blocked child task."""

        async with self._lock:
            self._reservations.pop(reservation.token, None)
            agent = self._agents.pop(reservation.agent_id, None)
            if agent is not None:
                agent.cancellation_requested = True
                task_handle = agent.task_handle
        task_handle.cancel()
        await asyncio.gather(task_handle, return_exceptions=True)

    async def complete(
        self,
        agent_id: str,
        status: AgentStatus,
        result: str | None,
    ) -> None:
        async with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None or agent.status is not AgentStatus.RUNNING:
                return
            if agent.cancellation_requested:
                status = AgentStatus.CANCELLED
                result = None
            agent.status = status
            agent.result = result
            agent.completion_event.set()

    async def check(
        self,
        context: DelegationContext,
        agent_id: str,
        timeout_seconds: float | None,
    ) -> AgentSnapshot | None:
        async with self._lock:
            agent = self._direct_agent(context, agent_id)
            if agent is None:
                return None
            snapshot = agent.snapshot()
            completion_event = agent.completion_event
        if timeout_seconds is None or snapshot.status is not AgentStatus.RUNNING:
            return snapshot
        try:
            await asyncio.wait_for(completion_event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            pass
        async with self._lock:
            agent = self._direct_agent(context, agent_id)
            return None if agent is None else agent.snapshot()

    async def list_direct(self, context: DelegationContext) -> tuple[AgentSnapshot, ...]:
        async with self._lock:
            return tuple(
                agent.snapshot()
                for agent in sorted(self._agents.values(), key=lambda item: item.sequence)
                if agent.owner_run_id == context.root_owner_run_id
                and agent.parent_agent_id == context.current_agent_id
            )

    async def steer(
        self,
        context: DelegationContext,
        agent_id: str,
        message: str,
    ) -> bool | None:
        async with self._lock:
            agent = self._direct_agent(context, agent_id)
            if agent is None:
                return None
            if agent.status is not AgentStatus.RUNNING or agent.cancellation_requested:
                return False
            agent.steering.append(message)
            return True

    async def drain_steering(self, agent_id: str) -> list[str]:
        async with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                return []
            messages = list(agent.steering)
            agent.steering.clear()
            return messages

    async def close_direct(
        self,
        context: DelegationContext,
        agent_id: str,
    ) -> AgentSnapshot | None:
        async with self._lock:
            selected = self._direct_agent(context, agent_id)
            if selected is None:
                return None
            if selected.status is not AgentStatus.RUNNING:
                return selected.snapshot()
            selected_ids = self._subtree_ids(selected.owner_run_id, selected.agent_id)
        await self._cancel_ids((selected.agent_id, *selected_ids))
        async with self._lock:
            selected = self._direct_agent(context, agent_id)
            return None if selected is None else selected.snapshot()

    async def close_descendants(self, context: DelegationContext) -> None:
        async with self._lock:
            selected_ids = tuple(
                agent.agent_id
                for agent in self._agents.values()
                if agent.owner_run_id == context.root_owner_run_id
                and self._is_descendant_of(agent, context.current_agent_id)
            )
        await self._cancel_ids(selected_ids)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._reservations.clear()
            selected_ids = tuple(self._agents)
        await self._cancel_ids(selected_ids)

    def _direct_agent(
        self,
        context: DelegationContext,
        agent_id: str,
    ) -> _SpawnedAgent | None:
        agent = self._agents.get(agent_id)
        if (
            agent is None
            or agent.owner_run_id != context.root_owner_run_id
            or agent.parent_agent_id != context.current_agent_id
        ):
            return None
        return agent

    def _subtree_ids(self, owner_run_id: str, parent_agent_id: str) -> tuple[str, ...]:
        descendants: list[str] = []
        frontier = {parent_agent_id}
        while frontier:
            children = {
                agent.agent_id
                for agent in self._agents.values()
                if agent.owner_run_id == owner_run_id and agent.parent_agent_id in frontier
            }
            descendants.extend(sorted(children))
            frontier = children
        return tuple(descendants)

    def _is_descendant_of(
        self,
        agent: _SpawnedAgent,
        parent_agent_id: str | None,
    ) -> bool:
        if parent_agent_id is None:
            return True
        current = agent
        while current.parent_agent_id is not None:
            if current.parent_agent_id == parent_agent_id:
                return True
            ancestor = self._agents.get(current.parent_agent_id)
            if ancestor is None or ancestor.owner_run_id != agent.owner_run_id:
                break
            current = ancestor
        return False

    async def _cancel_ids(self, agent_ids: tuple[str, ...]) -> None:
        async with self._lock:
            tasks: list[asyncio.Task[None]] = []
            for agent_id in agent_ids:
                agent = self._agents.get(agent_id)
                if agent is None or agent.status is not AgentStatus.RUNNING:
                    continue
                agent.cancellation_requested = True
                tasks.append(agent.task_handle)
        for task_handle in tasks:
            task_handle.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            for agent_id in agent_ids:
                agent = self._agents.get(agent_id)
                if agent is not None and agent.status is AgentStatus.RUNNING:
                    agent.status = AgentStatus.CANCELLED
                    agent.result = None
                    agent.completion_event.set()


class AgentRuntime:
    """Cwd-scoped owner for Agent Definitions, Runs, and Subagent lifetimes."""

    def __init__(
        self,
        definitions: Mapping[str, AgentDefinition],
        *,
        stable_instructions: str,
    ) -> None:
        self._definitions = dict(definitions)
        self._stable_instructions = stable_instructions
        self._registry = AgentRegistry()
        self._executions: dict[str, RunInvocation] = {}
        self._contexts: dict[str, DelegationContext] = {}
        self._spawn_transactions: set[asyncio.Event] = set()
        self._closed = False
        self._lock = asyncio.Lock()

    async def before_run(
        self,
        invocation: RunInvocation,
        context: object | None,
    ) -> Mapping[str, object]:
        if context is None:
            delegation = DelegationContext(
                root_owner_run_id=invocation.run_id,
                current_run_id=invocation.run_id,
                current_session_id=invocation.session_id,
                current_agent_id=None,
                depth=0,
            )
        elif isinstance(context, _AgentLineage):
            delegation = DelegationContext(
                root_owner_run_id=context.root_owner_run_id,
                current_run_id=invocation.run_id,
                current_session_id=invocation.session_id,
                current_agent_id=context.current_agent_id,
                depth=context.depth,
            )
        else:
            raise TypeError("unsupported Agent Run lifecycle context")
        async with self._lock:
            if self._closed:
                raise RuntimeError("Agent runtime is closed")
            self._executions[invocation.run_id] = invocation
            self._contexts[invocation.run_id] = delegation
        return {"runtime": self, "context": delegation}

    async def after_run(
        self,
        invocation: RunInvocation,
        context: object | None,
    ) -> None:
        del context
        async with self._lock:
            delegation = self._contexts.get(invocation.run_id)
        if delegation is not None:
            await self._registry.close_descendants(delegation)
        async with self._lock:
            self._executions.pop(invocation.run_id, None)
            self._contexts.pop(invocation.run_id, None)

    async def spawn(
        self,
        context: DelegationContext,
        task: str,
        agent_type: str | None,
        model: str | None,
    ) -> dict[str, str] | ToolFailure:
        task = task.strip()
        if not task:
            return ToolFailure("invalid_task: delegated task must not be empty")
        if context.depth >= MAX_DELEGATION_DEPTH:
            return ToolFailure(
                f"delegation_depth_exceeded: maximum depth is {MAX_DELEGATION_DEPTH}"
            )
        if agent_type is None:
            definition = DEFAULT_AGENT_DEFINITION
        else:
            definition = self._definitions.get(agent_type)
            if definition is None:
                return ToolFailure(f"unknown_agent_type: {agent_type}")
            if definition.disable_model_invocation:
                return ToolFailure(f"model_invocation_disabled: {agent_type}")
        async with self._lock:
            invocation = self._executions.get(context.current_run_id)
        if invocation is None:
            return ToolFailure("unknown_agent: invoking Run is no longer active")
        try:
            child_tools = invocation.tools.select(definition.tools)
        except KeyError as error:
            return ToolFailure(f"unavailable_agent_tool: {error.args[0]}")
        effective_model = _optional_text(model) or definition.model or invocation.selected_model
        transaction_finished = asyncio.Event()
        async with self._lock:
            if self._closed:
                return ToolFailure("agent_runtime_closed: runtime is shutting down")
            self._spawn_transactions.add(transaction_finished)
        try:
            return await self._spawn_child(
                invocation=invocation,
                context=context,
                definition=definition,
                child_tools=child_tools,
                task=task,
                effective_model=effective_model,
            )
        finally:
            transaction_finished.set()
            async with self._lock:
                self._spawn_transactions.discard(transaction_finished)

    async def _spawn_child(
        self,
        *,
        invocation: RunInvocation,
        context: DelegationContext,
        definition: AgentDefinition,
        child_tools: ToolRegistry,
        task: str,
        effective_model: str | None,
    ) -> dict[str, str] | ToolFailure:
        try:
            reservation = await self._registry.reserve()
        except _AgentCapacityError:
            return ToolFailure(
                f"agent_capacity_exceeded: maximum running Subagents is {MAX_RUNNING_SUBAGENTS}"
            )
        except _AgentRegistryClosedError:
            return ToolFailure("agent_runtime_closed: runtime is shutting down")

        try:
            child_handle = await create_session(
                invocation.storage,
                model=effective_model,
                origin="subagent",
                parent_session_id=context.current_session_id,
            )
        except BaseException:
            await self._registry.release(reservation)
            raise

        start_gate = asyncio.Event()
        child_hooks = self._child_hooks(invocation.hooks, reservation.agent_id)
        child_dispatcher = invocation.dispatcher.with_registry(child_tools)
        child_model_info = (
            invocation.model_info if effective_model == invocation.selected_model else None
        )
        lineage = _AgentLineage(
            root_owner_run_id=context.root_owner_run_id,
            current_agent_id=reservation.agent_id,
            depth=context.depth + 1,
        )

        async def run_child() -> None:
            try:
                await start_gate.wait()
                _, result = await send_message(
                    child_handle,
                    task,
                    storage=invocation.storage,
                    settings=invocation.settings,
                    model=invocation.model,
                    model_info=child_model_info,
                    tools=child_tools,
                    dispatcher=child_dispatcher,
                    stable_instructions=_child_instructions(
                        self._stable_instructions,
                        definition,
                    ),
                    max_steps=invocation.max_steps,
                    hooks=child_hooks,
                    lifecycle=self,
                    lifecycle_context=lineage,
                )
            except asyncio.CancelledError:
                await self._registry.complete(
                    reservation.agent_id,
                    AgentStatus.CANCELLED,
                    None,
                )
            except Exception as error:
                await self._registry.complete(
                    reservation.agent_id,
                    AgentStatus.FAILED,
                    f"run_failed: {type(error).__name__}",
                )
            else:
                if result.status is RunStatus.COMPLETED:
                    await self._registry.complete(
                        reservation.agent_id,
                        AgentStatus.COMPLETED,
                        result.output,
                    )
                elif result.status is RunStatus.CANCELLED:
                    await self._registry.complete(
                        reservation.agent_id,
                        AgentStatus.CANCELLED,
                        None,
                    )
                elif result.status is RunStatus.MAX_STEPS:
                    await self._registry.complete(
                        reservation.agent_id,
                        AgentStatus.FAILED,
                        "max_steps_exhausted",
                    )
                else:
                    assert result.error is not None
                    await self._registry.complete(
                        reservation.agent_id,
                        AgentStatus.FAILED,
                        f"run_failed: {type(result.error).__name__}",
                    )

        child_coroutine = run_child()
        try:
            task_handle = asyncio.create_task(child_coroutine)
        except BaseException:
            child_coroutine.close()
            await self._registry.release(reservation)
            await invocation.storage.rollback_empty_subagent(child_handle.session_id)
            raise
        try:
            activated = await self._registry.activate(
                reservation,
                context=context,
                task=task,
                session_id=child_handle.session_id,
                task_handle=task_handle,
            )
        except BaseException:
            await self._registry.abort_spawn(reservation, task_handle)
            await invocation.storage.rollback_empty_subagent(child_handle.session_id)
            raise
        if not activated:
            await self._registry.abort_spawn(reservation, task_handle)
            await invocation.storage.rollback_empty_subagent(child_handle.session_id)
            return ToolFailure("agent_runtime_closed: runtime is shutting down")
        start_gate.set()
        return {"agent_id": reservation.agent_id}

    async def check(
        self,
        context: DelegationContext,
        agent_id: str,
        timeout_seconds: float | None,
    ) -> dict[str, object] | ToolFailure:
        if timeout_seconds is not None and (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > MAX_CHECK_TIMEOUT_SECONDS
        ):
            return ToolFailure(
                "invalid_arguments: timeout_seconds must be finite, greater than zero, "
                f"and at most {MAX_CHECK_TIMEOUT_SECONDS:g}"
            )
        snapshot = await self._registry.check(context, agent_id, timeout_seconds)
        if snapshot is None:
            return ToolFailure(f"unknown_agent: {agent_id}")
        return snapshot.public()

    async def steer(
        self,
        context: DelegationContext,
        agent_id: str,
        message: str,
    ) -> dict[str, object] | ToolFailure:
        message = message.strip()
        if not message:
            return ToolFailure("invalid_arguments: steering message must not be empty")
        queued = await self._registry.steer(context, agent_id, message)
        if queued is None:
            return ToolFailure(f"unknown_agent: {agent_id}")
        if not queued:
            return ToolFailure(f"agent_not_running: {agent_id}")
        return {"agent_id": agent_id, "queued": True}

    async def list_agents(self, context: DelegationContext) -> list[dict[str, object]]:
        return [
            snapshot.public(include_task=True)
            for snapshot in await self._registry.list_direct(context)
        ]

    async def close_agent(
        self,
        context: DelegationContext,
        agent_id: str,
    ) -> dict[str, object] | ToolFailure:
        snapshot = await self._registry.close_direct(context, agent_id)
        if snapshot is None:
            return ToolFailure(f"unknown_agent: {agent_id}")
        return {"agent_id": snapshot.agent_id, "status": snapshot.status.value}

    async def close(self) -> None:
        """Stop all running Subagents before other cwd-scoped transports close."""

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            transactions = tuple(self._spawn_transactions)
        await self._registry.close()
        if transactions:
            await asyncio.gather(*(transaction.wait() for transaction in transactions))

    def _child_hooks(self, hooks: Hooks | None, agent_id: str) -> Hooks:
        active_hooks = hooks or Hooks()

        async def inject_messages() -> list[str]:
            existing = (
                await active_hooks.inject_messages()
                if active_hooks.inject_messages is not None
                else []
            )
            return [*existing, *(await self._registry.drain_steering(agent_id))]

        return Hooks(
            before_tool_call=active_hooks.before_tool_call,
            before_run_complete=active_hooks.before_run_complete,
            inject_messages=inject_messages,
        )


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _child_instructions(base: str, definition: AgentDefinition) -> str:
    ending = "" if definition.system_prompt.endswith("\n") else "\n"
    section = (
        "--- BEGIN AGENT DEFINITION ---\n"
        f"{definition.system_prompt}{ending}"
        "--- END AGENT DEFINITION ---"
    )
    return f"{base}\n\n{section}" if base else section
