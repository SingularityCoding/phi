# Chapter 10 · Events & Tracing

<div class="phi-chapter-meta" markdown>
<span>概念讲解</span><span>不现场写代码</span><span>对照 phi/harness/events.py、phi/harness/run.py、phi/sessions/service.py、phi/sessions/trace.py、phi/ui/app.py</span>
</div>

mini-agent 唯一的可观测手段是 `print()`。你自己盯着终端跑一次任务,这就够用了。但只要出现下面任何
一种情况,`print()` 就不够了:任务跑完之后想回头排查到底发生了什么;同一次运行需要同时喂给好几种不
同的界面展示;或者你需要拿出证据证明某次运行确实做了什么、没做什么。这一章讲的是:怎么把"发生了什
么"变成一份结构化的、可以被复用和验证的记录,而不是一堆滚动过去就没了的文本。

## 动手:看一次 Run 的原始事件流

Claude Code 有 `--output-format stream-json` 这类机器可读输出;Phi 也有:

```bash
uv run phi run "写一句话总结这个项目" --json
```

逐行看 `run_started` → `model_call_started` → `model_call_delta`(很多条)→
`model_call_completed` → `run_finished`,和接下来第一步要讲的 `RunEvent` 顺序对上。

## 观察行为和影响行为要分开

一旦你开始往循环里加"通知外部发生了什么"的机制,很容易顺手也让这个机制去改变行为——比如某个监听
者判断"这一步有问题"就直接改写了结果。这是一个危险的混淆:观察不应该有副作用。上一章讲的 Hook 才
是允许改变行为的机制,这一章的 Event 只负责如实通知。

## 主流做法

- **print 式调试**:我们目前一直在用的方式,足够简单,但没有结构、无法回放、无法多路复用。
- **结构化 JSON 日志**:每条记录是结构化数据而不是自由文本,更适合程序化处理。
- **分布式追踪系统**:OpenTelemetry 这类 span 式追踪,擅长跨服务的调用链路,但概念模型更重。
- **专门的 Agent 评测/可观测性平台**:面向 Agent 场景做了专门优化,通常是外部托管服务。

## Phi 怎么做:让同一组 Event 服务多个观察者

这一章要回答一个具体问题:Harness 发出的同一组 Event,怎样同时支持实时界面、测试观察和落盘
Trace,又不会让任何观察者反过来修改 Run?

在 VS Code 中打开以下文件:

```text
phi/src/phi/harness/events.py
phi/src/phi/harness/run.py
phi/src/phi/sessions/service.py
phi/src/phi/sessions/trace.py
phi/src/phi/ui/app.py
```

一条 Event 的路径:

```text
Harness 中发生状态变化(harness/run.py 里的 emitter.emit(...))
  → 创建类型化 Event 快照
  → EventBus 按顺序投递
      ├── SafeStepRecorder:取消时恢复完整 Step
      ├── TraceWriter:脱敏后写入 JSONL
      └── 外部 Host:实时渲染或输出 JSONL
```

### 第一步:一个 Run 能产生哪些 Event

在 `harness/events.py` 中找到 `RunEvent`。Phi 用一个封闭联合类型表示 Run 生命周期:

```python
type RunEvent = (
    RunStarted
    | ModelCallStarted
    | ModelCallDelta
    | ModelCallCompleted
    | ToolCallStarted
    | ToolCallCompleted
    | ApprovalDecided
    | RunFinished
)
```

每个 Event 都携带 `run_id` 和严格递增的 `event_index`;发生在具体 Step 中的 Event 还带
`step_index`。对一次包含 Tool Call 的两步 Run,主干顺序是:

```text
RunStarted
ModelCallStarted → ModelCallDelta* → ModelCallCompleted
ToolCallStarted → ApprovalDecided? → ToolCallCompleted
ModelCallStarted → ModelCallDelta* → ModelCallCompleted
RunFinished
```

这里记录的是领域事件,而不是已经拼好的界面文本——消费者可以根据同一份事实自己决定怎么展示。发出
位置就是 `harness/run.py` 里 `emitter.emit(...)` 的各处调用,和第 08 章看到的 `emitter.next_index()`
配合,给同一个 Run 内的全部 Event 分配唯一顺序号。

### 第二步:Event 是不能修改行为的冻结快照(和上一章的核心对照)

回到 `harness/events.py`,看各 Event 的 `__post_init__()`:

```python
def __post_init__(self) -> None:
    object.__setattr__(self, "call", freeze_tool_call(self.call))
    object.__setattr__(self, "result", freeze_tool_result(self.result))
```

`frozen=True` 只冻结 dataclass 顶层,`freeze_request()`、`freeze_response()` 等函数还会递归冻结
内部的消息列表、参数字典和 Tool Result。监听器拿到的是观察快照,不能通过修改嵌套 wire data 改变
随后要执行的 Tool Call 或最终 `RunResult`。再看 `EventListener` 的类型:

```python
# 监听器返回值刻意被忽略:Event 是通知,不能成为改变 Harness 行为的 Hook。
type EventListener[TEvent: Event] = Callable[[TEvent], Awaitable[object] | object]
```

这一行注释就是本章和上一章的分界线:需要改变控制流的扩展必须进入 `Hooks`,不能借 Event listener
隐式介入。

### 第三步:一个展示组件挂了,不该拖垮正在运行的 Run

`EventBus.emit()` 按订阅顺序逐个等待监听器:

```python
for listener in self._listeners:
    try:
        result = listener(event)
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception:
        continue
```

顺序投递让 Trace 和测试得到确定的 Event 次序。一个普通 listener 的异常被隔离,后面的观察者仍能收
到同一个 Event,Run 也不会因为展示代码出错而失败;`CancelledError` 属于任务控制流,必须继续传播,
否则界面发出的取消可能被观察层吞掉。

### 第四步:同一份 Event,同时喂给 Recorder、Trace 和界面

在 `sessions/service.py` 的 `_run_event_bus()` 里,一次 `send_message()` 固定接入两个内部消费
者,外部 Host 是可选的第三个:

```python
recorder = _SafeStepRecorder()
trace_writer = TraceWriter(storage.trace_path(handle.session_id))
listeners: list[EventListener[RunEvent]] = [recorder, trace_writer]
if external is not None:
    listeners.append(external.emit)
```

`_SafeStepRecorder` 从 Event 中拼装已经完整结束的 Step,用于取消后的安全持久化;`TraceWriter` 把
脱敏后的记录写进 JSONL(细节见下面的深入阅读)。外部 Host 排在最后,只负责展示。在 `ui/app.py`
中,`PhiApp.emit()` 用 `(run_id, step_index)` 做键,把同一组 Event 路由到界面组件:

```text
ModelCallStarted   → 创建 Step 视图
ModelCallDelta     → 追加流式文本或推理内容
ModelCallCompleted → 完成响应视图
ToolCallStarted    → 创建 Tool 视图
ToolCallCompleted  → 填入 Tool Result
RunFinished        → 清理空占位符
```

`phi run --json` 则把同一个 `serialize_run_event()` 结果输出为 JSONL。TUI、CLI、Trace 和测试共
享的是同一份事件语义,只是各自拥有展示或持久化格式——这也是第 11 章"Host 必须薄"的一个具体例证。

??? note "深入阅读(课后):Trace 怎样脱敏与批量落盘"

    `TraceWriter.__call__()` 把每个 Event 投影为稳定 JSON schema,再统一脱敏:

    ```python
    record["payload"] = _redact(_event_payload(event))
    ```

    `_redact()` 对命中 `api_key`、`authorization`、`cookie`、`password`、`secret`、`token` 等
    凭据语义的键整值替换;字符串内部的 Bearer token、`sk-` 开头的 key 和常见"键: 值"写法也会被
    正则遮蔽,超长文本还会被截断。

    高频的 `ModelCallDelta` 最多按 64 条批量写入,遇到其他边界 Event 会立即连同之前的增量一起刷
    盘,真正的阻塞文件 I/O 通过 `asyncio.to_thread()` 执行,并在追加后 `fsync`。Trace 是尽力而为
    的观测产品,不是 Session 的恢复来源——`tests/sessions/test_trace.py` 会故意破坏 Trace 文件,
    再验证 Conversation 仍然可以从 Session Entries 恢复。完整实现见
    `phi/src/phi/sessions/trace.py` 中的 `TraceWriter` 和 `_redact()`。

### 读完这条主线后

现在应该能够沿源码回答以下问题:

1. `run_id`、`event_index` 和 `step_index` 分别解决什么定位问题?
2. 为什么 Event 记录的是领域事实,而不是已经拼好的界面文本?
3. `frozen=True` 之外,为什么还需要 `freeze_request()` 这类递归快照?`EventListener` 的返回值为
   什么被忽略?
4. listener 的普通异常和 `CancelledError` 为什么采用不同处理策略?
5. Recorder、Trace 和界面怎样同时消费同一组 Event,又互不干扰?

## 讨论

Event 的数据在创建时就被冻结,这个设计防住的是"事件发出后被悄悄改写"。那如果问题出在更早的一
步——事件本身在创建时读到的就是一个已经被污染或者过期的状态,冻结机制还能保护什么吗?你觉得这种更
早阶段的问题,应该在哪一层被拦住?

??? success "展开参考答案"

    冻结只能保证 Event 创建之后不会被修改,不能保证创建时的数据就是正确的。把错误或过期状态冻结下
    来,只会得到一份不可修改的错误记录。

    这类问题应在拥有真实状态的组件中处理:从 Environment、Harness 或 Session 读取状态时使用明确
    的一致性边界,在创建 Event 前校验类型、不变量和版本,并尽量从同一个状态快照生成事件。Event bus
    负责传递事实,不应该反过来猜测或修正生产者的数据。
