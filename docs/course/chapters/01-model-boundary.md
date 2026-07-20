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
uv run python -c "
import asyncio
from settings import load_settings
from model import request

async def main():
    r = await request(load_settings(), [{'role': 'user', 'content': '说个笑话'}])
    print(r)

asyncio.run(main())
"
```

能看到一个 `content` 不是 `None` 的 `ModelResponse`,这一章就算通了。

## 对照阅读:看看 Phi 怎么做

打开 `phi/src/phi/model/openai_compatible.py`。同样是"模型边界",真实 Phi 的版本多了不少东西,挑几处
看看:

- **Streaming**:今天我们只发了一次性请求。真实 Phi 走 SSE,要一边收 `data:` 开头的行一边拼内容,还要
  确认流真的以 `[DONE]` 结尾——不然模型半路断线你都不知道。
- **一个真实的坑**:同一个 tool call 在连续几个 SSE chunk 里,有的供应商会把完整的 `id`/`name` 重复
  发好几遍,而不是只在第一个 chunk 给。Phi 里专门有一段逻辑,从第二次出现开始把 `id`/`name` 丢掉、只
  留参数片段——这不是过度设计,是踩过真实的坑之后加的。
- `usage` 字段里还会去掏 `prompt_tokens_details`/`completion_tokens_details` 这种供应商特有的嵌套字
  段(缓存了多少 token、推理用了多少 token)——我们今天的版本压根没管这个。
- 错误映射那里有个"已知的几个 context-length 错误码"列表——不同供应商报"上下文超了"用的字符串不一
  样,得挨个认。

这些多出来的部分,没有一处是"炫技",都是某个真实场景逼出来的。今天没写,不是因为不重要,是因为一节
课的时间,先把"边界该长什么样"这件事搞清楚,比一次性搞定所有供应商怪癖更值。

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
内部信任的 `ModelResponse`。

如果 Phi 以后直接支持 Anthropic Messages 或 OpenAI Responses，应该为它们增加新的 Model adapter，
把结果归一化成同一套内部类型，而不是让 Harness 到处判断当前使用的是哪家协议。
