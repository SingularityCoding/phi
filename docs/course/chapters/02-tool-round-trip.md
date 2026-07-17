# Chapter 02 · Tool Round Trip

<div class="phi-chapter-meta" markdown>
<span>TOOLS</span><span>约 2.5 小时</span><span>能力边界</span>
</div>

模型可以提出 Tool Call，但它没有执行权限。本章把一次手工工具往返变成可验证的 Harness
能力。

## 完整往返

```text
ModelRequest + tool schemas
            │
            ▼
      Model proposes call
            │
            ▼
Harness validates and executes
            │
            ▼
Tool Result with original call ID
            │
            ▼
       next ModelRequest
```

这里最重要的不是“成功调用一个 Python 函数”，而是执行权始终在 Harness 手中。

## 本章目标

- 由 Tool Registry 统一保存工具定义；
- 向 Model 暴露 JSON Schema，而不是 Python handler；
- 校验模型提供的参数；
- 区分 unknown tool、invalid arguments 与 handler exception；
- 保持 Tool Call ID 和 Tool Result 的配对。

## 需要实现

正式 Starter Edition 会在 `src/phi/tools/` 中提供本章切口：

- [ ] 实现 `Tool` 与 `ToolRegistry`；
- [ ] 拒绝重复注册的工具名称；
- [ ] 实现 Dispatcher 的查找和参数校验；
- [ ] 把 sync handler 适配到 async dispatcher 边界；
- [ ] 为 handler 设置明确 timeout；
- [ ] 把预期工具失败编码成结构化 Tool Result。

=== "Unknown tool"

    Model 请求一个 Registry 中不存在的名称。Harness 返回可恢复的工具失败，不能偷偷选择
    “最接近”的工具。

=== "Invalid arguments"

    参数没有通过 schema 校验。Handler 不应被调用，错误信息应足以让 Model 修正下一次请求。

=== "Handler exception"

    工具已经开始执行后抛出异常。Harness 必须区分确定失败与副作用状态不明，不能盲目重试。

## 验证

```bash
uv run pytest tests/course/test_chapter_02.py
```

重点观察：

- handler 在非法参数下调用次数必须为零；
- Tool Result 使用原始 `call_id`；
- Model 看到的是公开 schema，不是可信注入参数；
- expected tool failure 不会让整个 Python 进程崩溃。

## 完成标准

- Tool schemas 可以随下一次 ModelRequest 发送；
- 所有执行都通过同一个 Dispatcher；
- Model 无法绕过 Registry 直接调用 handler；
- 能解释“模型提出，Harness 授权”的含义。

## 故障实验

让 Scripted Model 连续请求：

1. 一个不存在的工具；
2. 一个参数类型错误的工具；
3. 一个会抛异常的工具；
4. 一个成功工具。

记录每次 Tool Result，确认 Harness 没有把前三种失败误认为 Agent Run 已完成。
