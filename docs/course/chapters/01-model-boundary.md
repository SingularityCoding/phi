# Chapter 01 · Model Boundary

<div class="phi-chapter-meta" markdown>
<span>FOUNDATIONS</span><span>约 2.5 小时</span><span>离线测试优先</span>
</div>

这一章只解决一个问题：**Phi 如何完成一次可信、可测试的模型请求？**

## 本章目标

- 把 Model 定义为无状态协议边界；
- 区分远端 wire dictionary 与 Phi 内部可信类型；
- 规范化文本、Tool Calls、Usage 与 finish reason；
- 用 Scripted Model 替代不稳定的在线模型测试。

## 先建立边界

```text
Harness
   │  ModelRequest
   ▼
Model Protocol
   │  OpenAI-compatible HTTP / SSE
   ▼
LiteLLM Proxy
```

Model 负责传输和协议规范化，但不拥有 conversation history、Context assembly、工具执行、
retry policy 或 UI。对 Model 来说，每次调用都是一次独立请求。

!!! question "为什么不让 Model 保存 messages？"

    如果 Model 同时保存历史，Harness 就无法准确知道每次请求实际携带了什么，也无法独立
    测试 Context 构建、会话分支和 compaction。无状态边界让这些责任保持可观察。

## 需要实现

正式 Starter Edition 会在 `src/phi/model/` 中留下本章实现切口。本页先展示预期任务形状：

- [ ] 定义 `ModelRequest`、`ModelResponse`、`ToolCall` 与 `Usage`；
- [ ] 定义只包含 `request()` 与 `request_stream()` 的 `Model` Protocol；
- [ ] 将非法 Tool Call JSON 转换为明确的 protocol error；
- [ ] 实现按顺序消费响应脚本的 `ScriptedModel`；
- [ ] 记录 Scripted Model 收到的每一个请求；
- [ ] 响应脚本耗尽时立即失败，而不是重复最后一项。

### 关键接口

```python
class Model(Protocol):
    async def request(self, request: ModelRequest) -> ModelResponse: ...
    def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
```

内部值使用完整类型；OpenAI-compatible dictionary 只停留在 adapter 边界。不要让整个项目
都依赖不受约束的 `dict[str, Any]`。

## 验证

最终 Starter Repository 中，本章会提供类似下面的独立测试入口：

```bash
uv run pytest tests/course/test_chapter_01.py
```

测试应检查：

1. Scripted Model 是否记录了完整请求；
2. 响应是否按顺序消费；
3. Tool Call arguments 是否已经解析；
4. Usage 缺失时是否保持为 `None`；
5. script exhaustion 是否明确失败。

## 完成标准

当以下条件都满足时，本章完成：

- 所有本章离线测试通过；
- Model 对 conversation history 一无所知；
- 测试不需要 API key 或网络；
- 能解释为什么 wire format 不应该扩散到 Harness 内部。

## 思考题

`finish_reason` 应该建模成封闭的 Enum，还是保留为开放字符串？如果 Proxy 新增一个 Phi
还不认识的值，两种设计分别会发生什么？
