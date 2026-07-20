# Chapter 11 · Hosts

<div class="phi-chapter-meta" markdown>
<span>概念讲解</span><span>不现场写代码</span><span>对照 phi/cli/、phi/ui/app.py、phi/sessions/service.py</span>
</div>

mini-agent 只有一种运行方式:一个 CLI 脚本,从头跑到尾。但真实世界里,Agent 会被从很多不同的界面
使用——一次性的终端命令、交互式的终端界面、有时候还有 IDE 面板或者网页聊天窗口。如果每种界面都各
自实现一遍"怎么调用模型、怎么执行工具、怎么管理会话",这些实现会不可避免地慢慢跑偏,同一个操作在
不同界面下表现得不一样。这一章讲的是:怎么支持多种使用界面,同时只维护一份真正的 Agent 行为。

## 界面和行为要不要长在一起

一旦一段业务逻辑被直接写进某个界面的代码里(比如某个命令的处理函数),它就很难被另一个界面复用。
下次要加一种新的使用方式,要么复制一遍逻辑,要么被迫做一次痛苦的重构。更好的做法是从一开始就把
"界面怎么呈现"和"实际发生了什么"分开。

## 主流做法

- **只有命令行工具**:实现简单,但缺少交互式、持续会话式的使用体验。
- **网页聊天界面**:适合大众用户,但通常意味着一整套独立的前后端。
- **IDE 内嵌面板**:比如 VS Code 插件形态,贴近开发者的日常工作流。
- **交互式终端界面(TUI)**:Phi 和 Claude Code 自己的交互模式都走这条路,同时也各自额外提供一条
  非交互的、可脚本化的命令行路径。

## Phi 怎么做：让不同 Host 共享同一份 Agent 行为

这一章对照同一条用户消息的两个入口：`phi run` 怎样执行一次无界面任务，Textual TUI 又怎样流式展示
同一个任务？阅读目标是找出它们在哪里分开、在哪里汇合，以及业务状态最终属于谁。

先在 VS Code 中打开以下文件：

```text
phi/src/phi/cli/main.py
phi/src/phi/cli/headless.py
phi/src/phi/ui/app.py
phi/src/phi/sessions/service.py
```

两条调用链可以先并排看：

```text
phi run TASK                         Textual 输入框提交
  → run_command                       → _run_one_message
  → execute_headless_run              → send_message
  → send_message                      → PhiApp.emit 实时渲染 Event
  → 输出最终结果                     → 更新 transcript 与状态栏
```

两条路径在 `send_message()` 汇合。汇合之前属于 Host 适配，汇合之后由 Session 服务和 Harness 共同完成
Agent 行为。

### 第一步：先界定 Headless Host 负责的输入适配

在 `cli/main.py` 中找到 `run_command()`。Typer callback 负责读取 `TASK`、`--session`、`--model`、
`--max-steps` 和 `--json`，并把它们交给 `execute_headless_run()`。

普通模式只呈现最终输出；JSON 模式则创建 `_JsonlEventWriter`，消费上一章的共享 Run Event。退出码也在
最外层由 `RunStatus` 映射。这些决定属于 CLI 协议：它们改变的是进程如何接收和呈现结果，不改变 Agent
下一步做什么。

### 第二步：沿 execute_headless_run 找到共享服务边界

切换到 `cli/headless.py`，查看 `execute_headless_run()`。它依次完成 Host 需要的装配工作：

```text
验证命令输入
→ 构建 cwd 作用域的 HostRuntime
→ 恢复已有 Session 或准备创建新 Session
→ 按显式参数、Session、默认配置解析 Model
→ 调用共享 Session 服务
→ 关闭普通 CLI 独占的运行时资源
```

关键边界是这次调用：

```python
handle, result = await send_message(
    handle,
    task,
    storage=runtime.storage,
    settings=runtime.settings,
    model=runtime.model,
    tools=runtime.resources.tools,
    dispatcher=runtime.resources.dispatcher,
    ...,
)
```

Headless Host 没有构造 Conversation View、执行 Agent loop 或自行追加 Session Entry。它把已经解析好的
依赖交给 `send_message()`，再接收新的不可变 `SessionHandle` 和 `RunResult`。

### 第三步：在 TUI 中找到同一个汇合点

打开 `ui/app.py`，找到 `_run_one_message()`。TUI 在调用共享服务前做的是界面状态管理：建立一个
`UserMessageView`、标记当前 Run、准备取消所需的 asyncio task，并选择当前 Model。

内部的 `execute()` 最终调用同一个 `send_message()`：

```python
updated, result = await send_message(
    self.current_session,
    text,
    ...,
    hooks=Hooks(inject_messages=self._inject_messages),
    events=self,
    lifecycle=runtime.resources.agents,
)
```

TUI 额外注入 `inject_messages`，把界面里的 Steer 队列接到 Harness Step 边界；同时固定通过
`events=self` 实时消费 Run Event。Headless 只在 `--json` 模式下注入 Event writer。两者使用的都是
Harness 已定义的公开边界，没有复制控制循环。

### 第四步：进入 send_message 确认真正的行为归属

现在切换到 `sessions/service.py`，找到 `send_message()`。从这里开始，两种 Host 走完全相同的路径：

```text
持久化 UserMessageEntry
→ 从 Session 投影 Conversation View 和 Context
→ 判断是否需要 Compaction
→ 调用 Harness run()
→ 把完整 Step 转成 Session Entries
→ 返回更新后的 SessionHandle 与 RunResult
```

因此 Context 预算、Tool Call 审批、重试、Compaction、Subagent 生命周期和持久化都不属于 CLI 或 TUI。
Host 换成 IDE 面板或 Web 页面时，这些行为仍然保持一致。

这里也能看出不可变 handle 的作用：服务返回 `updated`，TUI 再执行
`self.current_session = updated`；Host 不会直接修改 Session 元数据或 leaf pointer。

### 第五步：看 TUI 怎样把 Event 变成视图

继续在 `ui/app.py` 中找到 `PhiApp.emit()`。它根据 Event 类型更新界面：

```python
if isinstance(event, ModelCallStarted):
    ...
elif isinstance(event, ModelCallDelta):
    ...
elif isinstance(event, ToolCallStarted):
    ...
elif isinstance(event, ToolCallCompleted):
    ...
```

这里可以创建 `ModelStepView`、追加流式文本、展示 Tool Result，但 listener 没有返回任何会被 Harness 使用
的决定。Run 的控制与终态属于 Harness，Session 持久化 Conversation Entries；TUI 保存的
`_step_views` 只是短生命周期的显示索引。

取消也遵守同一边界：Escape 取消 `_active_run_task`，`send_message()` 负责把已完成 Step 安全落盘并
返回 `CANCELLED` 终态。界面不自行猜测哪些部分已经成功执行。

### 第六步：用 fork 验证“两个入口，一份行为”

在 `cli/main.py` 中找到 `session_fork_command()`，再回到 `ui/app.py` 的 `_execute_command()` 搜索
`/fork`。

CLI 路径：

```python
forked = _run_async(
    fork_session(storage, source, entry_id, model=selected_model)
)
```

TUI 路径：

```python
self.current_session = await fork_session(
    runtime.storage,
    handle,
    entry_id,
)
```

两边各自负责参数来源和结果展示，但都把分支点校验、谱系记录和新 Session 创建交给
`fork_session()`。这就是判断 Host 是否足够薄的可操作标准：同一业务动作跨 Host 时，应能指向同一个
服务入口。

### 第七步：标出 Host 可以拥有的状态

薄 Host 仍然需要状态，只是这些状态不能成为 Agent 行为的权威来源。以 TUI 为例：

```text
可以属于 Host                     应属于共享服务或 Harness
──────────────────────────────    ──────────────────────────────
输入框草稿与补全                  Session Entry 与 leaf pointer
队列行和当前选中的弹窗            Context 投影与 Compaction
Event 对应的临时 Step View         Tool Call 审批、执行和重试
滚动位置、状态栏、快捷键           Run 终态与持久化
```

只要一段逻辑会改变 Run、Session、Environment 或 Approval Policy 决策，或者多个 Host 必须得到完全
相同的结果，它就应该位于共享层，而不是某个具体界面中。

### 读完这条主线后

现在应该能够沿源码回答以下问题：

1. `run_command()` 与 `execute_headless_run()` 各自负责哪一层适配？
2. headless 与 TUI 在哪个函数汇合，汇合后发生哪些共享行为？
3. TUI 为什么可以注入 Steer、实时展示 Event，却仍然不拥有 Agent loop？
4. Session 服务为什么返回新的 `SessionHandle`，而不是让 Host 修改旧对象？
5. CLI 的 `session fork` 和 TUI 的 `/fork` 怎样共享同一份业务实现？
6. 判断一段状态或逻辑是否应留在 Host 的标准是什么？

## 讨论

"Host 必须保持薄"这条规则,在实践中最容易被打破的地方往往不是明显的业务逻辑,而是一些看起来像纯展
示、其实悄悄夹带了判断逻辑的代码(比如某个界面自己决定"这种情况要不要重试")。如果你要在代码评审
里守住这条边界,你会用什么具体的标准去判断一段代码到底该放在 Host 里,还是该被拉回共享服务里?

??? success "展开参考答案"

    一个实用的判断标准是：如果这段代码改变了 Run、Session、Environment 或 Approval Policy 决策，
    或者不同 Host 必须得到同样的结果，它就不属于 Host。

    Host 可以解析用户输入、调用共享服务、订阅 Event、管理临时界面状态并渲染结果；它不应该自行决定
    是否重试、是否批准工具或下一步执行什么。还可以用“换掉这个 Host”来检验边界：改用 CLI、TUI 或
    headless 后，核心行为应保持一致，变化的只应是输入和展示方式。
