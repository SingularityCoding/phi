"""管理 cwd 作用域内 Subagent Delegation 的注册、隔离与生命周期。"""

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
    """Subagent 在运行时注册表中的有限状态。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class DelegationContext:
    """绑定到一次确切调用 Run 的可信身份与 Delegation 谱系。"""

    root_owner_run_id: str
    current_run_id: str
    current_session_id: str
    current_agent_id: str | None
    depth: int


@dataclass(frozen=True)
class _AgentLineage:
    """在创建 child Run 前传递的最小父系信息。"""

    root_owner_run_id: str
    current_agent_id: str
    depth: int


@dataclass(frozen=True)
class AgentSnapshot:
    """一个 Subagent 可安全离开注册表锁的不可变快照。"""

    agent_id: str
    task: str
    status: AgentStatus
    result: str | None
    session_id: str

    def public(self, *, include_task: bool = False) -> dict[str, object]:
        """投影为 Model Tool 可返回的公共字段。"""

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
    """两阶段 spawn 在创建 Session 前预占的容量与稳定顺序。"""

    token: str
    agent_id: str
    sequence: int


@dataclass
class _SpawnedAgent:
    """注册表内部维护的可变 Subagent 生命周期记录。"""

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
        """复制当前公共状态，避免锁外观察可变记录。"""

        return AgentSnapshot(
            agent_id=self.agent_id,
            task=self.task,
            status=self.status,
            result=self.result,
            session_id=self.session_id,
        )


class _AgentCapacityError(Exception):
    """并发 Subagent 数已达到固定上限。"""

    pass


class _AgentRegistryClosedError(Exception):
    """关闭后的注册表拒绝新 Reservation。"""

    pass


class AgentRegistry:
    """并发安全、cwd 作用域的运行中与终止 Subagent 索引。

    这是内存索引而非持久化来源；完整 child 对话与观测分别保存在 Session 和 Trace。
    """

    def __init__(self) -> None:
        """初始化两阶段 Reservation、Agent 索引与串行化锁。"""

        self._agents: dict[str, _SpawnedAgent] = {}
        self._reservations: dict[str, _Reservation] = {}
        self._next_sequence = 0
        self._closed = False
        self._lock = asyncio.Lock()

    async def reserve(self) -> _Reservation:
        """原子预占一个并发名额、Agent ID 与展示顺序。"""

        async with self._lock:
            if self._closed:
                raise _AgentRegistryClosedError
            running = sum(agent.status is AgentStatus.RUNNING for agent in self._agents.values())
            # Reservation 也计入容量，防止并发 spawn 在 activate 前共同越过上限。
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
        """释放尚未进入激活阶段的 spawn Reservation。"""

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
        """把有效 Reservation 原子转换为已注册 Subagent。"""

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
        """回滚任一 spawn 阶段，并等待仍被启动闸门阻塞的 child task。"""

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
        """仅一次地把运行中 Subagent 转换为终止状态并唤醒等待者。"""

        async with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None or agent.status is not AgentStatus.RUNNING:
                return
            if agent.cancellation_requested:
                # 显式 close 的语义优先于 child 几乎同时报告的正常完成。
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
        """读取直接 Subagent 状态，并可通过 Event 有界等待完成。"""

        async with self._lock:
            agent = self._direct_agent(context, agent_id)
            if agent is None:
                return None
            snapshot = agent.snapshot()
            completion_event = agent.completion_event
        if timeout_seconds is None or snapshot.status is not AgentStatus.RUNNING:
            return snapshot
        try:
            # Event 等待不会轮询，也不会在 timeout 后取消 child task。
            await asyncio.wait_for(completion_event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            pass
        async with self._lock:
            agent = self._direct_agent(context, agent_id)
            return None if agent is None else agent.snapshot()

    async def list_direct(self, context: DelegationContext) -> tuple[AgentSnapshot, ...]:
        """按 spawn 顺序列出当前 Agent 的直接 Subagents。"""

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
        """向运行中的直接 Subagent 排入一条非破坏性 steering 消息。"""

        async with self._lock:
            agent = self._direct_agent(context, agent_id)
            if agent is None:
                return None
            if agent.status is not AgentStatus.RUNNING or agent.cancellation_requested:
                return False
            agent.steering.append(message)
            return True

    async def drain_steering(self, agent_id: str) -> list[str]:
        """原子取走 Subagent 下一 Step 边界应注入的全部消息。"""

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
        """关闭一个直接 Subagent 及其同一所有权树中的后代。"""

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
        """关闭属于指定 Run/Agent 的所有未结束后代。"""

        async with self._lock:
            selected_ids = tuple(
                agent.agent_id
                for agent in self._agents.values()
                if agent.owner_run_id == context.root_owner_run_id
                and self._is_descendant_of(agent, context.current_agent_id)
            )
        await self._cancel_ids(selected_ids)

    async def close(self) -> None:
        """关闭注册表，并取消等待全部仍运行的 Subagents。"""

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
        """按根 Run 所有权与直接父关系限制 Agent 可见范围。"""

        agent = self._agents.get(agent_id)
        if (
            agent is None
            or agent.owner_run_id != context.root_owner_run_id
            or agent.parent_agent_id != context.current_agent_id
        ):
            return None
        return agent

    def _subtree_ids(self, owner_run_id: str, parent_agent_id: str) -> tuple[str, ...]:
        """广度遍历指定父节点下同一根 Run 的全部后代 ID。"""

        descendants: list[str] = []
        frontier = {parent_agent_id}
        while frontier:
            # 每轮只展开当前 frontier 的直接孩子，避免跨所有权范围误取消。
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
        """判断记录是否位于指定父 Agent 之下。"""

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
        """先标记、再取消并等待任务，最后补齐未上报的 CANCELLED 状态。"""

        async with self._lock:
            tasks: list[asyncio.Task[None]] = []
            for agent_id in agent_ids:
                agent = self._agents.get(agent_id)
                if agent is None or agent.status is not AgentStatus.RUNNING:
                    continue
                agent.cancellation_requested = True
                tasks.append(agent.task_handle)
        for task_handle in tasks:
            # 不在持锁期间触发取消回调，避免 child complete() 反向等待同一把锁。
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
    """Agent Definitions、Runs 与 Subagent 生命周期的 cwd 作用域所有者。"""

    def __init__(
        self,
        definitions: Mapping[str, AgentDefinition],
        *,
        stable_instructions: str,
    ) -> None:
        """复制定义并初始化 Run 所有权、spawn 事务与注册表。"""

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
        """登记 Run 并构造注入 Delegation Tools 的可信 Context。"""

        if context is None:
            # Host 启动的顶层 Run 自己成为整棵 Delegation 树的根所有者。
            delegation = DelegationContext(
                root_owner_run_id=invocation.run_id,
                current_run_id=invocation.run_id,
                current_session_id=invocation.session_id,
                current_agent_id=None,
                depth=0,
            )
        elif isinstance(context, _AgentLineage):
            # child Run 继承根所有权，但使用自己的 Run、Session 和 Agent 身份。
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
        """在 Run 暴露终止结果前关闭其未完成后代并撤销登记。"""

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
        """校验 Delegation 请求并执行并发安全的 child spawn 事务。"""

        task = task.strip()
        if not task:
            return ToolFailure("invalid_task: delegated task must not be empty")
        if context.depth >= MAX_DELEGATION_DEPTH:
            # 深度在创建任何 Session 或 task 之前检查，失败不留下磁盘副作用。
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
                # 精确猜中隐藏定义也必须在 Model 边界拒绝，不能绕过目录过滤。
                return ToolFailure(f"model_invocation_disabled: {agent_type}")
        async with self._lock:
            invocation = self._executions.get(context.current_run_id)
        if invocation is None:
            return ToolFailure("unknown_agent: invoking Run is no longer active")
        try:
            # 显式 tools 是 child 白名单；None 由 ToolRegistry 解释为继承全部。
            child_tools = invocation.tools.select(definition.tools)
        except KeyError as error:
            return ToolFailure(f"unavailable_agent_tool: {error.args[0]}")
        effective_model = _optional_text(model) or definition.model or invocation.selected_model
        # 优先级：spawn 参数 > Agent Definition 偏好 > 父 Run 已选 Model。
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
            # shutdown 会等待所有处于 reserve/create/activate 中间态的事务结束。
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
        """以 Reservation、Session、task、activate 四阶段启动隔离 Subagent。"""

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
            # parent_session_id 仅记录 Delegation 谱系；child Context 从 task 开始。
        except BaseException:
            await self._registry.release(reservation)
            raise

        start_gate = asyncio.Event()
        # child task 在注册表 activate 成功前不得开始产生任何 Run 副作用。
        child_hooks = self._child_hooks(invocation.hooks, reservation.agent_id)
        child_dispatcher = invocation.dispatcher.with_registry(child_tools)
        child_model_info = (
            invocation.model_info if effective_model == invocation.selected_model else None
        )
        # Model 改变后旧 ModelInfo 不能冒充新模型容量信息；未知时走 best-effort。
        lineage = _AgentLineage(
            root_owner_run_id=context.root_owner_run_id,
            current_agent_id=reservation.agent_id,
            depth=context.depth + 1,
        )

        async def run_child() -> None:
            """执行标准 Session send_message，并把 RunResult 映射到 AgentStatus。"""

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
                # 取消在 child 边界转成终止状态，不让后台 task 泄漏取消异常。
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
                # Agent Registry 使用更粗粒度状态；Session/Trace 保留完整 child 细节。
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
            # shutdown 与 activate 竞态时完整回滚 task 和空 Session。
            await self._registry.abort_spawn(reservation, task_handle)
            await invocation.storage.rollback_empty_subagent(child_handle.session_id)
            return ToolFailure("agent_runtime_closed: runtime is shutting down")
        start_gate.set()
        # 返回 Agent ID 时 child 已在注册表中可查，但无需等待其任务完成。
        return {"agent_id": reservation.agent_id}

    async def check(
        self,
        context: DelegationContext,
        agent_id: str,
        timeout_seconds: float | None,
    ) -> dict[str, object] | ToolFailure:
        """校验等待上限并查询一个直接 Subagent。"""

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
        """为运行中的直接 Subagent 排队一条下一 Step 注入消息。"""

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
        """返回当前 Agent 的直接 Subagents，包括各自委派任务。"""

        return [
            snapshot.public(include_task=True)
            for snapshot in await self._registry.list_direct(context)
        ]

    async def close_agent(
        self,
        context: DelegationContext,
        agent_id: str,
    ) -> dict[str, object] | ToolFailure:
        """关闭并等待一个直接 Subagent 及其后代。"""

        snapshot = await self._registry.close_direct(context, agent_id)
        if snapshot is None:
            return ToolFailure(f"unknown_agent: {agent_id}")
        return {"agent_id": snapshot.agent_id, "status": snapshot.status.value}

    async def close(self) -> None:
        """在其他 cwd 作用域 transport 关闭前停止全部运行中 Subagents。"""

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            transactions = tuple(self._spawn_transactions)
        await self._registry.close()
        if transactions:
            # 同时进入的 spawn 事务负责自行回滚；运行时等待其完成再宣告关闭。
            await asyncio.gather(*(transaction.wait() for transaction in transactions))

    def _child_hooks(self, hooks: Hooks | None, agent_id: str) -> Hooks:
        """复用父 Hooks，并在 child Step 边界合并排队的 steering 消息。"""

        active_hooks = hooks or Hooks()

        async def inject_messages() -> list[str]:
            """先保留既有 Hook 注入，再追加该 Subagent 的 steering 队列。"""

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
    """把可选文本规范化为去空白字符串或 ``None``。"""

    if value is None:
        return None
    value = value.strip()
    return value or None


def _child_instructions(base: str, definition: AgentDefinition) -> str:
    """在稳定基础指令后追加边界清晰的 Agent Definition prompt。"""

    ending = "" if definition.system_prompt.endswith("\n") else "\n"
    section = (
        "--- BEGIN AGENT DEFINITION ---\n"
        f"{definition.system_prompt}{ending}"
        "--- END AGENT DEFINITION ---"
    )
    return f"{base}\n\n{section}" if base else section
