# Chapter 03 · Agent Loop

<div class="phi-chapter-meta" markdown>
<span>动手实现</span><span>mini-agent · agent.py</span><span>对照 phi/harness/run.py</span>
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

## 对照阅读:看看 Phi 怎么做

打开 `phi/src/phi/harness/run.py`。这是全课程里"多出来的复杂度"最集中的一处,挑三块看:

- **取消处理**:今天没有 streaming,所以也没有"一半发生任务被取消"这种情况——循环要么在两步之间(可
  以安全地停),要么根本没在跑。真实 Phi 因为要一边流式接收一边可能被用户按 Ctrl+C 打断,得非常小心
  地保证清理逻辑本身不会把"真正的取消"吞掉——这是这门课里最难讲清楚的一段异步代码,值得专门花时间
  过一遍。
- **Hooks**:真实 Loop 在"要不要真的结束这一轮"之前,会问一个外部注入的钩子——可以让钩子说"不行,
  接着干,这是反馈"而不是直接接受模型给出的答案。今天我们的版本里,模型说完就是完了。
- **typed Event 总线**:我们用 `print` 当作观察窗口,真实 Phi 发出的是一串结构化的 Event 对象,同一
  条流同时喂给 TUI、无头 CLI、测试和落盘的 Trace——这样加一个新的观察方式,不用改循环本身的代码。
