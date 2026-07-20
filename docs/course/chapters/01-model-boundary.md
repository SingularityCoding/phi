# Chapter 01 · Model 边界

<div class="phi-chapter-meta" markdown>
<span>动手实现</span><span>mini-agent · model.py</span><span>对照 phi/model/</span>
</div>

Agent 说到底是"模型 + 一个愿意听模型话、但自己拿主意的程序"。这一章我们先解决最基础的那一半:怎么
跟模型说上话,并且把它说的话变成程序能放心用的东西。

## 为什么这是第一步

模型接口本质上就是一次 HTTP 请求:发一段 JSON,收一段 JSON。难的从来不是"能不能连上",而是——收
回来的这段 JSON,格式是别人定的,你不能假设它总是干净、总是符合预期。今天要写的 `model.py`,就是在
"外面这个不受你控制的协议"和"你接下来所有代码都会用到的内部类型"之间,划一条清楚的界线。这条界线立
好了,后面 Tool 和 Loop 才能放心地只跟你自己的类型打交道,不用管 HTTP 层出了什么幺蛾子。

## 目标

- 把一次模型调用理解成:发送一个 `ModelRequest` 形状的东西,拿回一个 `ModelResponse`。
- 区分"网络/协议这一层出的错"和"模型好好回答了,只是内容你不喜欢"——前者是 `model.py` 的职责,后者
  不是。
- 拿到手的 `ModelResponse`,不管背后模型是哪个厂商、返回格式多古怪,都应该长一个样。

## 关键接口

```python
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

@dataclass(frozen=True)
class ModelResponse:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None

class ModelError(Exception): ...

async def request(
    settings: Settings,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> ModelResponse: ...
```

`settings.py` 是给好的,不用你操心怎么读环境变量。今天从 `request()` 开始写。

## 现场要写的东西

- [ ] `ToolCall` / `ModelResponse` / `ModelError`。
- [ ] 拼请求体,发一次真实的 `POST /chat/completions`(先别管 tools 参数,能收到一句纯文本回复就算过关)。
- [ ] 把网络失败、超时、非 2xx 状态码、响应体不是合法 JSON,统一收敛成 `ModelError`——`request()` 之外
  不应该看到任何原始的 `httpx` 异常或 `KeyError`。
- [ ] 从响应体里取出 `content`、`finish_reason`,以及 `tool_calls`(每个 call 要把 `arguments` 那个
  JSON 字符串 `json.loads` 成 dict,这一步出错也要变成 `ModelError`,不能让程序崩掉)。
- [ ] 写 `to_assistant_message` / `to_tool_message`,把 `ModelResponse` 和工具执行结果转回 wire 格
  式——这是反方向的转换,下一章接工具的时候会用到。

## 试一试

写完之后,拿真实的课程 proxy 跑一次:

```bash
uv run scripts/check_model.py
```

这个脚本是给好的,不用你写——它会测两条路径:一次不带 `tools` 参数的纯文本请求,一次带真实
工具 schema 的请求。第二条路径专门用来确认 `tool_calls` 那条分支真的走通了(`function.arguments`
从 JSON 字符串 `json.loads` 成 dict 这一步很容易漏),不要只满足于第一条能跑通。

## Phi 怎么做：把不可信协议变成稳定的内部类型

这一章的源码主线从一个边界问题开始：

> OpenAI-compatible 服务返回的是持续变化的 HTTP/SSE 数据，Harness 为什么可以只处理稳定的
> `ModelResponse`？

在 VS Code 中打开以下文件：

```text
phi/src/phi/model/types.py
phi/src/phi/model/openai_compatible.py
phi/src/phi/model/assembler.py
```

先记住完整的数据流：

```text
ModelRequest
    ↓ serialize
HTTP / SSE wire data
    ↓ validate and normalize
ModelEvent stream
    ↓ assemble
ModelResponse
```

网页只引用这条路径上的关键骨架，字段校验和异常分支以右侧源码为准。

### 第一步：先确认边界两侧的数据形状

在 `model/types.py` 中搜索 `ModelRequest` 和 `ModelResponse`。

`ModelRequest` 是 Harness 交给 Model adapter 的输入：

```python
@dataclass(frozen=True)
class ModelRequest:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
```

`messages` 和 `tools` 仍然保留 OpenAI-compatible wire shape，因为它们只在协议边界附近流动。返回值则
被归一化为 Phi 自己的类型：

```python
@dataclass(frozen=True)
class ModelResponse:
    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
```

这里要观察两个设计选择：

- `ToolCall.arguments` 已经是 `dict`，Harness 不需要解析供应商返回的 JSON 字符串。
- 文本、推理、Tool Call、Usage 和结束原因各有独立字段，Harness 不需要理解 SSE chunk。

因此 Model adapter 的输出不只是“请求成功后的 JSON”，而是结构稳定、协议有效的内部值。这不代表
Tool Call 已经获得执行信任；它仍然只是 Model 的提议，必须经过 Dispatcher 的参数校验和审批。

### 第二步：看请求如何进入 OpenAI-compatible 协议

切换到 `model/openai_compatible.py`，搜索 `_serialize_request()`。

```python
payload: dict[str, Any] = {
    "model": request.model or self._config.default_model,
    "messages": request.messages,
    "stream": stream,
}
if request.tools:
    payload["tools"] = request.tools
if request.temperature is not None:
    payload["temperature"] = request.temperature
```

没有设置的可选字段会被省略，而不是发送为 `null`。当 `stream=True` 时，Phi 还会显式请求 Usage：

```python
payload["stream_options"] = {"include_usage": True}
```

接着查看 `_request_events()`。HTTP 超时、网络错误、非 2xx 响应和非法 JSON 都在这里转换成
`ModelTimeoutError`、`ModelHTTPError` 或 `ModelProtocolError`。这保证 Harness 不会依赖 `httpx`，也
不会收到由 `KeyError` 或 `JSONDecodeError` 表示的协议失败。

错误归一化中还有一个更细的分支。搜索 `_http_error()`：只有结构化的 `error.code` 或 `error.type`
明确匹配已知 Context 上限错误时，Phi 才产生 `ModelContextLimitError`。

```python
structured_values = (raw_error.get("code"), raw_error.get("type"))
if any(
    isinstance(value, str) and value in context_limit_codes
    for value in structured_values
):
    return ModelContextLimitError(...)
```

自然语言错误消息不会被猜测分类。因为 Harness 会针对 Context overflow 采取专门的恢复策略，误分类可能
触发一次本不该发生的重试。

### 第三步：沿着 SSE 流检查传输是否完整

在同一文件中搜索 `_stream_events()`。

一次流式响应首先经过 `_iter_sse_data()`。它按照 SSE 的空行边界合并多条 `data:` 行，忽略注释和其他
字段，然后把每个完整事件交给 JSON 解析。

```text
HTTP response bytes
→ SSE event
→ JSON chunk
→ ContentDelta / ReasoningDelta / ToolCallDelta / FinishEvent / UsageEvent
```

`_stream_events()` 同时维护两个结束条件：

```python
saw_done = False
saw_finish_or_usage = False
```

- `[DONE]` 是 Provider 按协议发出的传输结束哨兵；缺失时，Phi 会把响应视为可能在中途被截断；
- `FinishEvent` 或 `UsageEvent` 证明流中出现了语义上的结束信号。

缺少任一条件都产生 `ModelProtocolError`，半截文本或半截 Tool Call 不会被当作成功响应交给 Harness。

继续观察 `seen_tool_call_indices`。同一个 Tool Call 的参数可能分散在多个 chunk 中，有些兼容服务还会在
后续 chunk 重复发送完整的 `id` 和 `name`。Phi 只保留首次出现的身份字段，后续 chunk 只贡献参数片段：

```python
if event.index in seen_tool_call_indices:
    event = ToolCallDelta(
        index=event.index,
        arguments_fragment=event.arguments_fragment,
    )
```

由此建立的不变量是：Tool Call 的 `id` 和 `name` 确定一次，`arguments_fragment` 始终按到达顺序追加。

### 第四步：把增量组装成一次完整响应

打开 `model/assembler.py`，搜索 `ResponseAssembler.absorb()`。

组装器为不同类型的增量维护独立状态：

```python
if isinstance(event, ContentDelta):
    ...
elif isinstance(event, ReasoningDelta):
    ...
elif isinstance(event, ToolCallDelta):
    buffer = self._tool_calls.setdefault(event.index, _ToolCallBuffer())
    ...
    buffer.arguments += event.arguments_fragment
```

这里的 `index` 是 Tool Call 分片的归属键。不同 Tool Call 即使交错到达，也会进入各自的 buffer；最终
`build()` 按 index 排序，恢复供应商声明的调用顺序。

进入 `_build_tool_call()` 后再看一次信任边界：

```python
arguments = json.loads(
    buffer.arguments,
    parse_constant=_reject_non_json_constant,
)
if not isinstance(arguments, dict):
    raise ModelProtocolError(...)
```

Phi 等所有参数片段到齐后才解析 JSON，并拒绝缺失的 `id`/`name`、非对象参数以及 Python 解码器默认会
接受的 `NaN`、`Infinity`。只有通过这些检查，数据才成为 `ToolCall`。

### 第五步：确认流式与非流式最终走到同一个出口

回到 `OpenAICompatibleModel.request()`：

```python
assembler = ResponseAssembler()
async for event in self.request_stream(request, _transport_stream=False):
    assembler.absorb(event)
return assembler.build()
```

普通 JSON 响应会先由 `_events_from_non_streaming_response()` 展开为同一组 `ModelEvent`，再交给同一个
Assembler。因此两条传输路径满足同一个契约：

```text
non-streaming JSON ─┐
                    ├─→ ModelEvent ─→ ResponseAssembler ─→ ModelResponse
streaming SSE ──────┘
```

这个统一出口很关键。否则新增字段或加强校验时，很容易只修复 Streaming 或 non-streaming 中的一条路。
`tests/model/test_openai_compatible.py` 和 `tests/model/test_assembler.py` 分别覆盖协议解析与组装不变量，可以
在读完实现后对照查看失败案例。

### 第六步：看归一化结果如何回到下一轮请求

最后在 `openai_compatible.py` 中搜索 `serialize_assistant_response()` 和
`serialize_tool_result()`。

Model 输出经过归一化后，下一轮仍需要重新编码为 OpenAI-compatible message。Tool Call 参数使用稳定的
JSON 编码：

```python
json.dumps(
    call.arguments,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
```

稳定编码让相同参数始终生成相同文本，便于 Context token 估算、快照比较和测试复现。Tool Result 则必须
携带原始 `call_id`，让 Provider 能把结果与此前的 Tool Call 配对。

### 读完这条主线后

现在应该能够沿源码回答以下问题：

1. 哪些 OpenAI-compatible 字段会穿过 Model 边界，哪些会被转换成 Phi 的内部类型？
2. 为什么收到 `[DONE]` 之前不能把已经出现的文本当成成功响应？
3. 为什么 Tool Call 参数要等流结束后再解析，而不是逐个 chunk 执行 `json.loads()`？
4. Streaming 与 non-streaming 怎样保证返回相同形状的 `ModelResponse`？
5. 为什么 Context overflow 只能根据结构化错误码判断？
6. `serialize_assistant_response()` 为什么要稳定地编码 Tool Call 参数？

## Wire format 不只有一种

我们今天实现的是 **OpenAI Chat Completions-compatible** 格式：请求发到
`/chat/completions`，输入是一组 `messages`，返回内容位于 `choices[].message`，Tool Call 也嵌在
assistant message 中。Phi 当前使用的就是这套格式。

这里的 “OpenAI-compatible” 描述的是通信协议，不代表背后运行的一定是 OpenAI 的模型。Phi 面向的是
LiteLLM Proxy；Proxy 可以在后面路由不同供应商的模型，再把它们统一成 Chat Completions 格式返回。

实际还会遇到另外两种常见格式：

- **Anthropic Messages API**：请求发到 `/v1/messages`，回复由一组 content blocks 组成，文本、
  `tool_use` 和 `tool_result` 都是不同类型的 block，system prompt 也有独立的顶层字段。
- **OpenAI Responses API**：请求发到 `/v1/responses`，使用类型化的 Items 表示 message、reasoning、
  function call 和 function call output，Streaming 返回的也是类型化 Event。

三种格式的字段和 Streaming 事件都不一样，但它们解决的是同一个边界问题：把外部协议转换成 Agent
内部结构稳定的 `ModelResponse`。

如果 Phi 以后直接支持 Anthropic Messages 或 OpenAI Responses，应该为它们增加新的 Model adapter，
把结果归一化成同一套内部类型，而不是让 Harness 到处判断当前使用的是哪家协议。
