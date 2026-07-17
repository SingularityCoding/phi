# Chapter 03 · Agent Loop

<div class="phi-chapter-meta" markdown>
<span>HARNESS</span><span>约 3 小时</span><span>Bounded control</span>
</div>

完成一次 Tool Round Trip 后，我们终于可以把它放进循环。但一个 `while True` 还不是工程上
可信的 Agent Harness。

## 本章目标

- 定义 Run、Step 和明确的 terminal status；
- 让每个循环步骤只完成一次 Model turn 及其工具往返；
- 支持多个 Tool Calls，并保持结果顺序与配对；
- 设置最大步数、取消与无进展停止；
- 发出 typed Events，而不是让 UI 读取 Harness 内部状态。

## 控制循环

```python
for step_number in range(max_steps):
    response = await model.request(context)

    if response.tool_calls:
        results = await dispatcher.execute_all(response.tool_calls)
        context = context.with_tool_results(results)
        continue

    return RunResult.completed(response.content)

return RunResult.max_steps()
```

这只是结构草图。真实实现还必须处理 streaming、取消、失败语义、Hooks 和 Event 顺序。

## 需要实现

正式 Starter Edition 会在 `src/phi/harness/` 中提供本章切口：

- [ ] 定义 Run 状态与 terminal result；
- [ ] 实现明确的 `max_steps`，禁止无界循环；
- [ ] 把 Model response 和 Tool Results 原子地追加到本次 Run 状态；
- [ ] 发出 `RunStarted`、Model、Tool 和 `RunFinished` Events；
- [ ] 取消未完成的网络或工具任务；
- [ ] 保证 terminal event 只发出一次。

## Events 不是 Hooks

| 机制 | 作用 | 可以改变行为吗 |
| --- | --- | --- |
| Event | 通知外部观察者发生了什么 | 不可以 |
| Hook | 在命名拦截点参与 Harness 决策 | 可以 |

Textual TUI、headless CLI、JSONL Trace 和测试可以消费同一条 Event stream。它们不应该各自
实现一套 Agent loop。

## 验证

```bash
uv run pytest tests/course/test_chapter_03.py
```

至少覆盖这些脚本：

| Scripted responses | 预期结果 |
| --- | --- |
| 一次普通文本 | `completed`，一个 Step |
| Tool Call → 文本 | `completed`，两个 Model turns |
| 每一步都请求工具 | `max_steps` |
| Model protocol error | typed failed Run |
| 执行期间取消 | `cancelled`，无遗留任务 |

## 完成标准

- Run 永远有明确的上界；
- 每条 Tool Result 都能追溯到 Tool Call；
- Host 不拥有或复制控制循环；
- terminal status 与最后一条 Event 一致；
- 能用 Scripted Model 重现每一种停止原因。

## 思考题

如果 Model 返回空文本、没有 Tool Call，但 `finish_reason="length"`，Harness 应该把它判定为
正常完成、可恢复失败还是不可恢复失败？这个决定需要哪些上下文？
