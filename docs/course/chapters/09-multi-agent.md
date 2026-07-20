# Chapter 09 · Multi-agent

<div class="phi-chapter-meta" markdown>
<span>概念讲解</span><span>不现场写代码</span><span>对照 phi/agents/、phi/harness/run.py、phi/sessions/service.py</span>
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

## Phi 怎么做：追踪一次 Subagent 委派

这一章从一次具体委派开始：父 Agent 调用 `spawn_agent` 之后，Phi 怎样创建一个真正隔离的 child
Session 和 Run，同时保证它仍然属于父 Agent 可以管理的一棵树？

先在 VS Code 中打开以下文件：

```text
phi/src/phi/agents/tools.py
phi/src/phi/agents/registry.py
phi/src/phi/harness/run.py
phi/src/phi/sessions/service.py
```

完整控制流可以先压缩成：

```text
父 Model 提议 spawn_agent
  → ToolDispatcher 完成审批与参数校验
  → AgentRuntime.spawn 校验深度、定义和可用 Tool 集合
  → reserve 容量
  → 创建 child Session 与后台 task
  → activate 注册
  → child 通过标准 send_message 启动独立 Run
  → 父 Agent 用 check / steer / list / close 管理直接孩子
```

### 第一步：从 Model 能看到的五个 Tool 开始

在 `agents/tools.py` 中找到 `build_agent_tools()`。它返回五个普通的 Phi Tool：

```text
spawn_agent  → 启动一个 Subagent，立即返回 agent_id
check_agent  → 查询状态，可在有界时间内等待
steer_agent  → 为下一个 Step 排入一条消息
list_agents  → 列出当前 Agent 的直接孩子
close_agent  → 取消并等待一个孩子及其后代
```

这些函数本身只把请求转交给 `AgentRuntime`。`AgentRuntime` 和 `DelegationContext` 通过
`Injected[...]` 注入，Model 只能提供 task、agent_id 等不可信参数，不能伪造自己的 Run、Session、
父子身份或 Delegation 深度。

注意 `spawn_agent` 的 approval class：

```python
@tool(
    name="spawn_agent",
    ...,
    approval_class=ApprovalClass.UNCONFINED,
)
```

委派发生在现有 Tool Call 审批边界之内。审批拒绝时，child Session 和后台任务都还没有创建。

### 第二步：用 DelegationContext 定义可见范围

切换到 `agents/registry.py`，先查看 `DelegationContext`：

```python
@dataclass(frozen=True)
class DelegationContext:
    root_owner_run_id: str
    current_run_id: str
    current_session_id: str
    current_agent_id: str | None
    depth: int
```

`AgentRuntime.before_run()` 为顶层 Run 创建根 Context；child Run 则继承
`root_owner_run_id`，换成自己的 Run、Session 和 Agent ID，并把深度加一。

随后查看 `AgentRegistry._direct_agent()`：一次管理操作只有同时匹配根 Run 所有权和直接父关系，才能
取得目标记录。

```python
if (
    agent is None
    or agent.owner_run_id != context.root_owner_run_id
    or agent.parent_agent_id != context.current_agent_id
):
    return None
```

所以 Agent ID 不是管理凭证。另一个顶层 Run 即使知道这个 ID，也不能查询、转向或关闭它；父 Agent
也只管理直接孩子，不能绕过中间节点操作任意后代。

### 第三步：在创建副作用前收紧 child 的能力边界

进入 `AgentRuntime.spawn()`。它在创建 Session 之前依次验证：

```text
task 非空
→ depth 小于 MAX_DELEGATION_DEPTH
→ 如果显式指定 agent_type，Agent Definition 必须存在且允许 Model 调用
→ 当前父 Run 仍然活跃
→ definition.tools 能从父 Run 的 ToolRegistry 中选出
→ 确定 child 使用的 Model
```

Model 的优先级由这行代码固定：

```python
effective_model = (
    _optional_text(model)
    or definition.model
    or invocation.selected_model
)
```

即显式 spawn 参数优先，其次是 Agent Definition，最后继承父 Run 已选择的 Model。可用 Tool 集合只能
从父 Agent 已有 Tool 中做白名单选择，child 不能通过 Definition 获得父 Agent 本来没有的能力。

### 第四步：跟踪 reserve、create、activate 三个状态边界

继续进入 `_spawn_child()`。`AgentRegistry.reserve()` 先在锁内预占容量、Agent ID 和稳定展示顺序：

```python
if running + len(self._reservations) >= MAX_RUNNING_SUBAGENTS:
    raise _AgentCapacityError
```

Reservation 也计入并发上限，因此多个并发 spawn 不能在注册前一起越过上限。接下来 Phi 创建独立的
child Session：

```python
child_handle = await create_session(
    invocation.storage,
    model=effective_model,
    origin="subagent",
    parent_session_id=context.current_session_id,
)
```

`parent_session_id` 只记录谱系，不复制父 Conversation。child 的第一条 User Message 是委派 task；
父 Session 中其他内容不会自动进入 child Context。

后台 task 创建后仍被 `start_gate` 挡住。只有 `activate()` 已把它原子注册进 Registry，Phi 才执行
`start_gate.set()`。因此 `spawn_agent` 返回 Agent ID 时，这个 ID 一定已经可查询；child 也不会在
注册失败时提前产生 Run 副作用。

### 第五步：确认 child 复用同一套 Agent 原语

查看 `_spawn_child()` 内部的 `run_child()`。真正启动 child 的仍然是 Session 服务：

```python
_, result = await send_message(
    child_handle,
    task,
    ...,
    hooks=child_hooks,
    lifecycle=self,
    lifecycle_context=lineage,
)
```

这意味着 child 拥有自己的 Session、Run 和 Trace，但继续复用同一个 Harness loop、ToolDispatcher、
Approval Policy 和 Session 持久化机制。Registry 只保存粗粒度的运行状态；完整对话与执行证据仍然由
Session 和 Trace 持有。

Registry 对外只暴露四个 `AgentStatus`：初始状态是 `RUNNING`；child Run 结束后，`RunResult` 被
映射为 `COMPLETED`、`FAILED` 或 `CANCELLED`。父 Agent 通过 `check_agent` 得到状态和最终结果，
不会把 child 的逐 token 输出混入自己的 transcript。

### 第六步：沿 steering 路径回到 Hook

在 `AgentRegistry.steer()` 中，消息只是被追加到 child 记录的队列：

```python
agent.steering.append(message)
```

再查看 `AgentRuntime._child_hooks()`。它把这条队列接到上一章的 `inject_messages` Hook：

```python
return [
    *existing,
    *(await self._registry.drain_steering(agent_id)),
]
```

因此 `steer_agent` 不会取消 child 当前的 Model 请求或 Tool Call。排队的消息保持顺序，只消费一次，并
在下一个 Step 边界成为新的 User 消息。

### 第七步：从父 Run 结束反查清理责任

最后回到 `AgentRuntime.after_run()`。父 Run 无论正常完成、失败还是取消，在结果暴露给调用方之前都会
执行：

```python
await self._registry.close_descendants(delegation)
```

`close_agent` 也会找出整棵后代子树，先标记取消，再取消并等待所有 task，最后补齐仍未上报的
`CANCELLED` 状态。这样父 Run 结束后不会留下失去所有者的后台 Agent。

### 读完这条主线后

现在应该能够沿源码回答以下问题：

1. 为什么知道一个 `agent_id` 仍不足以管理这个 Subagent？
2. child 的 Model 和可用 Tool 集合分别怎样确定？
3. `reserve` 与 `activate` 之间为什么还需要 `start_gate`？
4. child Session 为什么记录 `parent_session_id`，却不继承父 Conversation？
5. `steer_agent` 怎样复用 Hook，并保证消息只在 Step 边界生效一次？
6. 父 Run 结束时，谁负责取消和等待尚未完成的后代？

## 讨论

"非破坏性干预"(在下一个步骤边界注入消息,而不是立刻打断)相比"立刻打断重来",在哪些场景下明显更
好?又在哪些场景下,你会宁愿要一个能立刻打断的机制,即使它更容易出错?如果两种都要支持,设计上会
多出什么复杂度?

??? success "展开参考答案"

    非破坏性干预适合当前步骤代价较高、具有副作用或即将完成的场景：让它先到达一个稳定边界，可以保留
    已经完成的工作，也更容易维护一致的 Session 和 Trace。

    如果子 Agent 正在泄露密钥、删除错误目标，或者继续执行会迅速扩大损失，就更需要立即打断。

    同时支持两种方式后，系统还必须定义取消发生在哪些位置、工具执行到一半怎样清理或回滚、Session 如
    何记录未完成步骤、父 Agent 怎样确认干预已经生效，以及取消、完成和新消息同时到达时如何处理竞态。
