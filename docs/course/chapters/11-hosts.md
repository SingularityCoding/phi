# Chapter 11 · Hosts

<div class="phi-chapter-meta" markdown>
<span>概念讲解</span><span>不现场写代码</span><span>对照 phi/cli/main.py、phi/cli/headless.py、phi/ui/app.py、phi/sessions/service.py</span>
</div>

mini-agent 只有一种运行方式:一个 CLI 脚本,从头跑到尾。但真实世界里,Agent 会被从很多不同的界面
使用——一次性的终端命令、交互式的终端界面、有时候还有 IDE 面板或者网页聊天窗口。如果每种界面都各
自实现一遍"怎么调用模型、怎么执行工具、怎么管理会话",这些实现会不可避免地慢慢跑偏,同一个操作在
不同界面下表现得不一样。这一章讲的是:怎么支持多种使用界面,同时只维护一份真正的 Agent 行为。

## 动手:同一个 Session,换一个 Host

不用重新做一遍 fork——用第 05 章 fork 出来的(或者随便一个)Session ID,先在无界面模式下续一条
消息:

```bash
uv run phi run "用一句话补充一点你还没提到的信息" --session <上面的 session_id>
uv run phi session resume <同一个 session_id>
```

进入 TUI 后,你会看到刚才那条 headless 消息已经出现在 transcript 里——CLI 和 TUI 走的是同一个
Session,不是两套互不相通的存储。

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

## Phi 怎么做:让不同 Host 共享同一份 Agent 行为

这一章对照同一条用户消息的两个入口:`phi run` 怎样执行一次无界面任务,Textual TUI 又怎样流式展示
同一个任务?阅读目标是找出它们在哪里分开、在哪里汇合,以及业务状态最终属于谁。

在 VS Code 中打开以下文件:

```text
phi/src/phi/cli/main.py
phi/src/phi/cli/headless.py
phi/src/phi/ui/app.py
phi/src/phi/sessions/service.py
```

### 第一步:先立一个判断标准——什么状态或逻辑不该留在 Host

带着这张表去读接下来的代码,会比读完代码再总结更容易验证:

```text
可以属于 Host                     应属于共享服务或 Harness
──────────────────────────────    ──────────────────────────────
输入框草稿与补全                  Session Entry 与 leaf pointer
队列行和当前选中的弹窗            Context 投影与 Compaction
Event 对应的临时 Step View         Tool Call 审批、执行和重试
滚动位置、状态栏、快捷键           Run 终态与持久化
```

判断标准很直接:只要一段逻辑会改变 Run、Session、Environment 或 Approval Policy 决策,或者多个
Host 必须得到完全相同的结果,它就该在共享层,而不是某个具体界面里。

### 第二步:两条入口各自只做输入适配,然后交给同一个函数

`cli/headless.py` 的 `execute_headless_run()` 依次完成:验证输入、解析或创建 Session、按"显式
`--model` > 恢复的 Session > 运行时默认"解析 Model,然后:

```python
handle, result = await send_message(
    handle,
    task,
    storage=runtime.storage,
    settings=runtime.settings,
    model=runtime.model,
    tools=runtime.resources.tools,
    dispatcher=runtime.resources.dispatcher,
    stable_instructions=runtime.resources.stable_instructions,
    max_steps=max_steps,
    events=events,
    lifecycle=runtime.resources.agents,
)
```

`ui/app.py` 的 `_run_one_message()` 在调用前做的是界面状态管理(建立 `UserMessageView`、准备可
取消的 asyncio task),内部的 `execute()` 调用的是同一个函数:

```python
updated, result = await send_message(
    self.current_session,
    text,
    storage=runtime.storage,
    settings=runtime.settings,
    model=runtime.model,
    tools=runtime.resources.tools,
    dispatcher=runtime.resources.dispatcher,
    stable_instructions=runtime.resources.stable_instructions,
    max_steps=self._max_steps,
    hooks=Hooks(inject_messages=self._inject_messages),
    events=self,
    lifecycle=runtime.resources.agents,
)
self.current_session = updated
```

两处调用唯一的差别是 Host 特有的附加物:TUI 多传了 `hooks=Hooks(inject_messages=...)`(把界面
的 Steer 队列接到第 08 章的 Hook)和 `events=self`(实时渲染,第 10 章讲过的同一组 Run Event);
headless 只在 `--json` 模式下传一个 JSONL event writer。两者用的都是 Harness 已经定义好的公开
参数,没有另外复制一份控制循环。

### 第三步:进入 send_message,才是 Agent 行为真正发生的地方

`sessions/service.py` 的 `send_message()` 开头就是持久化,而不是先跑 Model:

```python
user_entry = UserMessageEntry(parent_id=handle.leaf_id, content=text)
# 用户消息在 Model 调用前先持久化，保证失败或取消后仍能恢复请求分支。
handle = await _append(storage, handle, (user_entry,), ...)
run_events, step_recorder, trace_writer = _run_event_bus(storage, handle, events)
try:
    return await _continue_send(active, ...)
```

`_continue_send()`(第 04 章讲过的 Compaction 判断就住在这里)接着完成 Context 投影、必要时的
压缩、调用 Harness `run()`、把完整 Step 提交为 Session Entry。这条链上没有一步属于 CLI 或
TUI——Context 预算、Tool Call 审批、重试、Compaction、Subagent 生命周期和持久化,换成 IDE 面板或
Web 页面时都保持一致。这里也能看出不可变 handle 的作用:服务返回 `updated`,调用方(不管是
headless 还是 TUI)只是把自己持有的引用换成新值,不会直接修改 Session 元数据或 leaf pointer。

### 第四步:用 fork 验证"两个入口,一份行为"

在 `cli/main.py` 的 `session_fork_command()` 里:

```python
forked = _run_async(fork_session(storage, source, entry_id, model=selected_model))
```

在 `ui/app.py` 的 `/fork` 命令里:

```python
self.current_session = await fork_session(runtime.storage, handle, entry_id)
```

两边各自负责参数来源(命令行参数 vs 交互式历史选择)和结果展示,但分支点校验、谱系记录和新
Session 创建全部交给同一个 `fork_session()`(第 05 章讲过的实现)。这是判断"Host 是否足够薄"最
短小的一个例证:同一业务动作跨 Host,应该能指向同一个服务入口。

### 读完这条主线后

现在应该能够沿源码回答以下问题:

1. 判断一段状态或逻辑是否该留在 Host,你会用第一步那张表里的哪条标准?
2. headless 与 TUI 在哪个函数汇合?汇合之前,两者各自多传了什么 Host 特有的参数?
3. `send_message()` 一开始就持久化用户消息,而不是先跑 Model,这个顺序在解决什么问题?
4. Session 服务为什么返回新的 `SessionHandle`,而不是让 Host 修改旧对象?
5. CLI 的 `session fork` 和 TUI 的 `/fork` 怎样共享同一份业务实现?

## 讨论

"Host 必须保持薄"这条规则,在实践中最容易被打破的地方往往不是明显的业务逻辑,而是一些看起来像纯展
示、其实悄悄夹带了判断逻辑的代码(比如某个界面自己决定"这种情况要不要重试")。如果你要在代码评审
里守住这条边界,你会用什么具体的标准去判断一段代码到底该放在 Host 里,还是该被拉回共享服务里?

??? success "展开参考答案"

    一个实用的判断标准是:如果这段代码改变了 Run、Session、Environment 或 Approval Policy 决策,
    或者不同 Host 必须得到同样的结果,它就不属于 Host。

    Host 可以解析用户输入、调用共享服务、订阅 Event、管理临时界面状态并渲染结果;它不应该自行决定
    是否重试、是否批准工具或下一步执行什么。还可以用"换掉这个 Host"来检验边界:改用 CLI、TUI 或
    headless 后,核心行为应保持一致,变化的只应是输入和展示方式。
