# Chapter 03 · Agent Loop

<div class="phi-chapter-meta" markdown>
<span>动手实现</span><span>mini-agent · agent.py</span><span>对照 phi/harness/run.py、events.py、hooks.py</span>
</div>

前两章分别搞定了"怎么跟模型说话"和"怎么安全地执行工具"。这一章把它们接起来,变成一个真正能自己
干活的循环——写完这一章,`main.py` 那个从第一节课就一直报错的地方,终于能跑通了。

## 循环长什么样

去掉细节,一个 Agent Loop 其实就是这几行:

```python
messages = [{"role": "user", "content": task}]
for step in range(max_steps):
    response = await model.request(settings, messages, tools=registry.specs())
    messages.append(to_assistant_message(response))
    if not response.tool_calls:
        return "completed", response.content
    for call in response.tool_calls:
        result = await dispatch(registry, call)
        messages.append(to_tool_message(call.id, result.output or result.error))
    if step + 1 == max_steps:
        return "max_steps", None
```

真正要写的东西比这个骨架多一点(状态怎么表示、每一步该往外报告什么),但核心逻辑就是这几行:问模
型、看它要不要工具、要就执行并把结果喂回去、不要就说明它做完了。真正麻烦的地方,是想清楚"什么时候
该停"。

## 目标

- 循环必须有上限——模型不配合、或者陷入死循环,程序也不能一直转下去。
- 结束的原因要明确区分:任务完成了、步数用完了、请求模型这一步直接失败了——这三种情况对使用者的意
  义完全不同,不能都表示成"结束了"。
- 每一步发生了什么,要有地方能看到——哪怕只是打印到终端,而不是一个黑盒子跑完才告诉你结果。

## 关键接口

```python
@dataclass(frozen=True)
class RunResult:
    status: str  # "completed" | "max_steps" | "failed"
    output: str | None
    error: str | None
    messages: list[dict[str, Any]]

async def run_agent(
    settings: Settings,
    task: str,
    registry: ToolRegistry,
    *,
    max_steps: int = 10,
    on_event: Callable[[str], None] = print,
) -> RunResult: ...
```

## 现场要写的东西

- [ ] `RunResult`,把三种结束状态和各自该有/不该有的字段(`output` 只在 completed 时有意义,`error`
  只在 failed 时有意义)想清楚。
- [ ] 主循环:请求模型 → 记录 assistant 消息 → 没有 tool_calls 就返回 completed → 有就逐个 `dispatch`
  并把结果写回 `messages` → 检查是不是到了 `max_steps`。
- [ ] 请求模型这一步失败(`ModelError`),要干净地变成 `status="failed"`,不能让异常从 `run_agent`
  里跑出去。
- [ ] 每一步至少打印一行,让人知道现在在干什么——没有 TUI,终端输出就是唯一的观察窗口。

## 试一试

```bash
uv run main.py "读一下 tools.py，告诉我这个项目内置了哪些工具" --max-steps 6
```

看着它自己决定"我需要先读文件",调用 `read_file`,再总结出答案——这是这门课到目前为止最值得停下来
看一眼的时刻。

## Phi 怎么做：追踪一个 Run 怎样前进并停止

这一章的源码主线是 Harness 最核心的控制权：

> Model 只会返回下一步建议，Phi 怎样把多次 Model 调用与 Tool 执行组成一个有界、可观察、可取消的
> Run？

在 VS Code 中打开以下文件：

```text
phi/src/phi/harness/run.py
phi/src/phi/harness/events.py
phi/src/phi/harness/hooks.py
```

先把一次 Run 压缩成控制流：

```text
initial ModelRequest
        ↓
  ┌─→ one Model call ─→ assembled ModelResponse
  │                           ├─ Tool Calls ─→ dispatch ─→ append Tool Results ─┐
  │                           └─ final text ─→ completion Hook ─→ RETRY ───────┤
  │                                                                            │
  └──────────────────────────── next Step ←────────────────────────────────────┘

任意路径都受同一个 max_steps 预算约束
```

网页中的摘录用于定位控制流的转折点，完整的异常清理和 Event 字段以右侧源码为准。

### 第一步：先看 Run 的输入、状态和终点

在 `harness/run.py` 中搜索 `RunStatus`、`Step` 和 `RunResult`。

一个 `Step` 记录一次完整的 Model 请求与响应，以及这一响应产生的全部 Tool Result：

```python
@dataclass(frozen=True)
class Step:
    index: int
    request: ModelRequest
    response: ModelResponse
    tool_results: tuple[ToolResult, ...] = ()
```

一个 Run 则有四种终态：

| 状态 | 含义 |
| --- | --- |
| `COMPLETED` | Model 给出最终文本；如果配置了完成 Hook，候选结果还需被它接受 |
| `MAX_STEPS` | 总 Step 预算已经用尽 |
| `FAILED` | Model、协议、Hook 或内部执行边界出现不可恢复错误 |
| `CANCELLED` | 外层服务记录的一次取消终态；底层 `run()` 通过传播取消来交出控制权 |

`RunResult.__post_init__()` 强制检查字段组合：只有 `COMPLETED` 能携带 `output`，只有 `FAILED` 能携带
`error`。因此 Host 不需要猜测“output 为 None 究竟是失败还是预算用尽”。

接着定位 `run()` 的参数。`Model` 是无状态请求边界，`ToolDispatcher` 是唯一执行边界；本次 Run 的
消息、Step、Hook 和 Event 序号都由 Harness 在函数内部拥有。

### 第二步：找到每个 Step 的固定起点

在 `run()` 中找到主循环：

```python
for step_index in range(max_steps):
    ...
    request = ModelRequest(
        messages=deepcopy(working_messages),
        tools=deepcopy(working_tools),
        model=initial_request.model,
        temperature=initial_request.temperature,
        max_tokens=initial_request.max_tokens,
    )
```

`max_steps` 不是 Tool Call 数量，而是本次 Run 允许进行的 Model 请求总数。每个 Step 开始时，Phi 都从
`working_messages` 创建独立的请求快照；这个 Step 尚未发生的 Tool Result 只能影响下一次请求。

循环顶部还会调用 `hooks.inject_messages()`。Host 在 Model 请求进行中收到的 steer 消息会排队，等下一个
Step 边界再作为 User 消息注入。它不会打断当前请求，也不会重新开启一个 Run。

这一步建立两个重要不变量：

- `initial_request` 是模板，不会随着循环推进被修改；
- 一个 Step 观察到的 request 在创建后保持稳定，可以可靠写入 Event 和 Trace。

### 第三步：看一次 Model 调用怎样成为完整 Step

继续沿主循环找到 `model.request_stream(request)`：

```python
assembler = ResponseAssembler()
stream = model.request_stream(request)
async for delta in stream:
    assembler.absorb(delta)
    await emitter.emit(ModelCallDelta(..., delta))
response = assembler.build()
```

Harness 先把 delta 交给 `ResponseAssembler`，再发出观察 Event。这样 Event 的顺序与最终响应的组装顺序
始终一致。只有流正常结束且 `assembler.build()` 成功后，Phi 才生成 `ModelCallCompleted`，并开始判断
Tool Call 或最终文本。

Model 请求、协议解析或响应组装抛出的普通异常都会变成 `RunStatus.FAILED`：

```python
except Exception as error:
    return await _finish(
        emitter,
        RunResult(RunStatus.FAILED, tuple(steps), error=error),
    )
```

失败前已经完成的 Step 会保留；当前仍是半成品的请求不会被虚构成 Step。

### 第四步：沿 Tool Call 分支回到下一次 Model 请求

找到 `if response.tool_calls:`。Phi v1 按 Model 返回的顺序处理同一响应内的全部 Tool Call：

```python
for call in response.tool_calls:
    ...
    result = await dispatcher.dispatch(
        deepcopy(call),
        approval_policy=active_hooks.before_tool_call,
        approval_observer=observe_approval,
    )
    tool_results.append(result)
```

Model 提议调用，Dispatcher 完成查找、校验、审批与执行，Harness 负责顺序和下一步。每个调用首先产生
`ToolCallStarted`；进入审批时可以产生 `ApprovalDecided`；Dispatcher 正常返回 `ToolResult` 后才产生
`ToolCallCompleted`。

全部 Tool Result 产生后，当前 Step 才会被记录。然后 Harness 将 Assistant 的 Tool Call 和配对结果追加
到工作消息：

```python
working_messages.append(serialize_assistant_response(response))
working_messages.extend(
    serialize_tool_result(result) for result in tool_results
)
```

下一次 Model 请求由此能够看到“自己提议了什么”和“Environment 实际返回了什么”。即使 Tool Result
携带 `unknown_tool`、`approval_denied` 或 `handler_error`，它仍然是一次已完成的 Tool 往返，Model 可以
在下一步修正策略。

如果 Dispatcher 意外抛出内部异常，Phi 会保留此前已经成功产生的 Tool Result，再让 Run 进入
`FAILED`。这使 Trace 能准确说明副作用执行到了哪里。

### 第五步：沿最终文本分支检查完成是否成立

没有 Tool Call 时，Model 给出的是一个“候选完成结果”。Harness 先记录当前 Step，再构造
`provisional_result`：

```python
output = response.content if response.content is not None else ""
provisional_result = RunResult(
    RunStatus.COMPLETED,
    tuple(steps),
    output=output,
)
```

如果没有 `before_run_complete` Hook，这就是最终结果。存在 Hook 时，打开 `harness/hooks.py`，查看
`CompletionDecision` 的两个选择：

```text
ACCEPT → 接受候选结果，Run 完成
RETRY  → 追加 Assistant 候选回答与 User 反馈，进入下一 Step
```

Hook 的 `RETRY` 不会启动一个新的 Run，也没有独立的无限重试预算。它与 Tool 往返共同消耗当前 Run 的
`max_steps`：

```python
working_messages.append(serialize_assistant_response(response))
working_messages.append({"role": "user", "content": decision.feedback})
if step_index + 1 == max_steps:
    return await _finish(... RunStatus.MAX_STEPS ...)
```

因此 Model、Tool 或 Hook 都不能绕过 Harness 拥有的停止条件。

### 第六步：把 Event 当成旁观者，把取消当成控制流

打开 `harness/events.py`，观察 Run 生命周期对应的类型化 Event：

```text
RunStarted
→ ModelCallStarted → ModelCallDelta* → ModelCallCompleted
→ (ToolCallStarted → ApprovalDecided? → ToolCallCompleted)*
→ RunFinished
```

`_EventEmitter` 为同一个 Run 绑定稳定的 `run_id`，并分配严格递增的 `event_index`。Event 携带冻结快照，
监听器可以驱动 TUI、headless 输出或 Trace，但不能修改正在运行的请求、响应或 Tool Call。

取消走的是另一条控制流。在 Model Streaming 中捕获到 `asyncio.CancelledError` 时，Phi 先关闭异步流，再
原样向外传播：

```python
except asyncio.CancelledError:
    try:
        await _close_stream(stream)
    except BaseException:
        pass
    raise
```

Dispatcher 和交互式 Approval 也都传播取消。底层 `run()` 不把它伪装成 `FAILED`，也不发出一个声称正常
结束的 `RunFinished`；拥有 Session 和 Host 生命周期的外层服务负责记录取消结果。

可以在 `tests/harness/test_run.py` 中对照 Tool 往返和 Event 顺序，在
`tests/harness/test_cancellation.py` 中查看 Model stream、审批等待和异步 Tool 三个取消位置。

### 读完这条主线后

现在应该能够沿源码回答以下问题：

1. `Step` 和 `RunResult` 分别保存什么，为什么半成品 Model 请求不能成为 Step？
2. `max_steps` 约束的是哪一种预算，Tool 往返和完成 Hook 怎样共同消耗它？
3. Tool Call、Assistant message 与 Tool Result 以什么顺序进入下一次 Model 请求？
4. 为什么预期 Tool 失败可以继续 Run，而 Dispatcher 的契约缺陷会让 Run 进入 `FAILED`？
5. Event 为什么只能观察，不能决定审批、重试或停止？
6. 为什么取消必须关闭当前流后继续传播，而不能转换成普通失败结果？
