# Chapter 09 · Multi-agent

<div class="phi-chapter-meta" markdown>
<span>概念讲解</span><span>不现场写代码</span><span>对照 phi/agents/tools.py、phi/agents/registry.py、phi/harness/run.py、phi/sessions/service.py</span>
</div>

到目前为止,我们讨论的一直是一个 Agent 在做一件事。但复杂任务经常天然可以拆开:一部分工作需要专门
的上下文或工具,一部分工作可以并行推进,如果全部挤在同一个循环、同一份历史里,Context 会迅速膨
胀,任务之间也很容易互相干扰。真正的问题是:一个 Agent 怎么把任务的一部分,交给另一个独立的 Agent
去做,同时还能保持对结果的掌控。

## 委派之后,谁还负责

一旦允许委派,马上会出现一堆需要回答的问题:子任务是共享父任务的状态,还是有自己独立的一份;父任
务能不能中途干预子任务;父任务能不能看到子任务执行过程中的每一个细节,还是只能等结果。不同的答案
对应完全不同的架构复杂度和风险面。

## 主流做法

- **完全不支持委派**:保持单一扁平的 Agent,简单,但没法拆分复杂任务。
- **把子 Agent 当成一个没有自主性的工具**:调用形式上像一次工具调用,但子 Agent 内部谈不上真正独立
  的运行状态。
- **完全对等的多 Agent 框架**:多个 Agent 是平等的节点,互相发消息、共享状态,AutoGen、CrewAI 这类
  框架走的是这个方向,灵活但协调复杂度也最高。
- **父-编排者/子-执行者模式**:父 Agent 派生出隔离的子 Agent,检查子 Agent 的结果,始终保持主导
  权——这是 Claude Code 自己的 Task 工具的形状,也是 Phi 的选择。

## Phi 怎么做:追踪一次 Subagent 委派

这一章从一次具体委派开始:父 Agent 调用 `spawn_agent` 之后,Phi 怎样创建一个真正隔离的 child
Session 和 Run,同时保证它仍然属于父 Agent 可以管理的一棵树?

在 VS Code 中打开以下文件:

```text
phi/src/phi/agents/tools.py
phi/src/phi/agents/registry.py
phi/src/phi/harness/run.py
phi/src/phi/sessions/service.py
```

完整控制流:

```text
父 Model 提议 spawn_agent
  → ToolDispatcher 完成审批与参数校验
  → AgentRuntime.spawn 校验深度、定义和可用 Tool 集合
  → 创建 child Session 与后台 task(细节见课后深入阅读)
  → child 通过标准 send_message 启动独立 Run
  → 父 Agent 用 check / steer / list / close 管理直接孩子
```

### 第一步:Model 能看到的五个 Tool,以及它们为什么不能伪造身份

在 `agents/tools.py` 中找到 `build_agent_tools()`。它返回五个普通的 Phi Tool:

```text
spawn_agent  → 启动一个 Subagent,立即返回 agent_id
check_agent  → 查询状态,可在有界时间内等待
steer_agent  → 为下一个 Step 排入一条消息
list_agents  → 列出当前 Agent 的直接孩子
close_agent  → 取消并等待一个孩子及其后代
```

每个函数只是把请求转交给 `AgentRuntime`:

```python
async def spawn_agent(
    task: NonEmptyText,
    runtime: Injected[AgentRuntime],
    context: Injected[DelegationContext],
    agent_type: NonEmptyText | None = None,
    model: NonEmptyText | None = None,
) -> object:
    return await runtime.spawn(context, task, agent_type, model)
```

`runtime` 和 `context` 都通过 `Injected[...]` 注入(第 06 章讲过的同一个机制)。Model 只能提供
task、agent_id 这些不可信参数,不能伪造自己的 Run、Session、父子身份或 Delegation 深度。
`spawn_agent` 的审批类别是 `ApprovalClass.UNCONFINED`,委派发生在现有 Tool Call 审批边界之内——
审批拒绝时,child Session 和后台任务都还没有创建。

### 第二步:agent_id 不是管理凭证——只认直接父子关系

切换到 `agents/registry.py`,先看 `DelegationContext`:

```python
@dataclass(frozen=True)
class DelegationContext:
    root_owner_run_id: str
    current_run_id: str
    current_session_id: str
    current_agent_id: str | None
    depth: int
```

`AgentRuntime.before_run()` 为顶层 Run 创建根 Context;child Run 继承 `root_owner_run_id`,换
成自己的 Run、Session 和 Agent ID,并把深度加一。真正做访问控制的是 `_direct_agent()`:

```python
def _direct_agent(self, context: DelegationContext, agent_id: str) -> _SpawnedAgent | None:
    agent = self._agents.get(agent_id)
    if (
        agent is None
        or agent.owner_run_id != context.root_owner_run_id
        or agent.parent_agent_id != context.current_agent_id
    ):
        return None
    return agent
```

一次管理操作(`check`/`steer`/`close`)必须同时匹配根 Run 所有权和直接父关系,才能取得目标记
录。所以知道一个 `agent_id` 不足以管理它:另一个顶层 Run 即使拿到这个 ID 也查不到;父 Agent 也只
管理直接孩子,不能绕过中间节点操作任意后代。

### 第三步:在创建任何副作用前,先收紧 child 的能力边界

进入 `AgentRuntime.spawn()`。它在创建 Session 之前依次验证:

```python
if context.depth >= MAX_DELEGATION_DEPTH:
    return ToolFailure(f"delegation_depth_exceeded: maximum depth is {MAX_DELEGATION_DEPTH}")
...
if definition.disable_model_invocation:
    return ToolFailure(f"model_invocation_disabled: {agent_type}")
...
child_tools = invocation.tools.select(definition.tools)
```

深度限制和 Tool 白名单是这里真正重要的两条:`MAX_DELEGATION_DEPTH` 防止委派链无限延伸;
`child_tools` 只能从父 Run 已有的 Tool 中挑选,child 不能通过 Agent Definition 获得父 Agent 本来
没有的能力。Model 使用的具体版本按"显式 spawn 参数 > Agent Definition 偏好 > 父 Run 已选
Model"的优先级解析,细节留给源码,不是这一章的重点。

### 第四步:child 复用的是同一套 Harness,不是第二个隐藏循环

这是本章真正的论点。进入 `_spawn_child()` 内部的 `run_child()`:

```python
_, result = await send_message(
    child_handle,
    task,
    storage=invocation.storage,
    settings=invocation.settings,
    model=invocation.model,
    tools=child_tools,
    dispatcher=child_dispatcher,
    stable_instructions=_child_instructions(self._stable_instructions, definition),
    max_steps=invocation.max_steps,
    hooks=child_hooks,
    lifecycle=self,
    lifecycle_context=lineage,
)
```

启动 child 的仍然是第 05/11 章讲过的同一个 `send_message()`。child 拥有自己的 Session、Run 和
Trace,但继续复用同一个 Harness loop、`ToolDispatcher`、Approval Policy 和 Session 持久化机制。
`AgentRegistry` 只保存粗粒度的运行状态——`RunResult` 结束后被映射为 `COMPLETED`、`FAILED` 或
`CANCELLED`;完整对话与执行证据仍然由 Session 和 Trace 持有,父 Agent 通过 `check_agent` 只拿到
状态和最终结果,不会把 child 的逐 token 输出混入自己的 transcript。

### 第五步:steer 怎样非破坏性地影响 child

在 `AgentRegistry.steer()` 中,消息只是被追加到 child 记录的队列:

```python
agent.steering.append(message)
```

再看 `AgentRuntime._child_hooks()`。它把这条队列接到第 08 章的 `inject_messages` Hook 上:

```python
async def inject_messages() -> list[str]:
    existing = (
        await active_hooks.inject_messages() if active_hooks.inject_messages is not None else []
    )
    return [*existing, *(await self._registry.drain_steering(agent_id))]
```

因此 `steer_agent` 不会取消 child 当前的 Model 请求或 Tool Call。排队的消息保持顺序,只消费一
次,并在下一个 Step 边界成为新的 User 消息——和父 Agent 自己被 Steer 时走的是同一条机制。

??? note "深入阅读(课后):spawn 的并发安全与父 Run 结束时的清理"

    `spawn()` 真正创建 child 之前,还有一层并发正确性问题:多个 `spawn_agent` 调用可能同时发
    生,不能一起越过 `MAX_RUNNING_SUBAGENTS` 上限。`AgentRegistry` 用三阶段解决:

    ```python
    reservation = await self._registry.reserve()      # 先占容量与 agent_id
    child_handle = await create_session(...)           # 再创建 Session
    activated = await self._registry.activate(...)     # 最后原子登记
    ```

    child 的后台 task 创建后仍被 `start_gate` 挡住,只有 `activate()` 成功把它登记进 Registry,
    Phi 才会 `start_gate.set()`。任何一步失败都会回滚已经做过的部分(释放 Reservation、取消已
    创建的 task、回滚空 Session)。这是通用的"预占-创建-激活"并发模式,不是 Agent 特有的概念。

    父 Run 结束时的清理对称地简单:无论正常完成、失败还是取消,`AgentRuntime.after_run()` 都会
    调用:

    ```python
    await self._registry.close_descendants(delegation)
    ```

    它找出整棵后代子树,先标记取消,再取消并等待所有 task,最后补齐仍未上报的 `CANCELLED` 状
    态,确保父 Run 结束后不会留下失去所有者的后台 Agent。完整实现见
    `phi/src/phi/agents/registry.py` 中的 `AgentRegistry.reserve()`/`activate()` 与
    `AgentRuntime.after_run()`。

### 读完这条主线后

现在应该能够沿源码回答以下问题:

1. 为什么知道一个 `agent_id` 仍不足以管理这个 Subagent?
2. child 的可用 Tool 集合怎样被限制在父 Agent 已有能力之内?
3. child Session 为什么记录 `parent_session_id`,却不继承父 Conversation?
4. child 复用了父 Agent 的哪些 Harness 组件?哪些是 child 独有的?
5. `steer_agent` 怎样复用 Hook,并保证消息只在 Step 边界生效一次?
6.(对应深入阅读)父 Run 结束时,谁负责取消和等待尚未完成的后代?

## 讨论

"非破坏性干预"(在下一个步骤边界注入消息,而不是立刻打断)相比"立刻打断重来",在哪些场景下明显更
好?又在哪些场景下,你会宁愿要一个能立刻打断的机制,即使它更容易出错?如果两种都要支持,设计上会
多出什么复杂度?

??? success "展开参考答案"

    非破坏性干预适合当前步骤代价较高、具有副作用或即将完成的场景:让它先到达一个稳定边界,可以保留
    已经完成的工作,也更容易维护一致的 Session 和 Trace。

    如果子 Agent 正在泄露密钥、删除错误目标,或者继续执行会迅速扩大损失,就更需要立即打断。

    同时支持两种方式后,系统还必须定义取消发生在哪些位置、工具执行到一半怎样清理或回滚、Session 如
    何记录未完成步骤、父 Agent 怎样确认干预已经生效,以及取消、完成和新消息同时到达时如何处理竞态。
