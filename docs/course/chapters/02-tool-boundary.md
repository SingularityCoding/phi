# Chapter 02 · Tool 边界

<div class="phi-chapter-meta" markdown>
<span>动手实现</span><span>mini-agent · tools.py</span><span>对照 phi/tools/</span>
</div>

模型能说出"我要读一下 main.py",但它自己没有手。谁来真正打开这个文件、谁来决定这个请求合不合理、
出了问题谁来兜底——这一章都是在回答这些问题。

## 两件事分开想

工具其实有两张脸:**模型看到的**(名字、描述、参数长什么样),和**真正会跑的那段代码**。这两者必须
分开——模型永远不该拿到一个 Python 函数的引用,它只能"提议"调用某个名字、带着某些参数,剩下的事情
全部由你的程序说了算。今天写的 `tools.py`,就是把这句话变成代码。

## 目标

- 用一个 Registry 统一管理"有哪些工具、每个工具的 schema 长什么样"。
- 有一个唯一的执行入口(`dispatch`),所有工具调用都得走这里,不能有第二条路。
- 把"模型瞎编了个不存在的工具名""模型给的参数不对""工具执行到一半自己炸了"这三种情况清楚地分开——
  它们对 Harness 来说是完全不同的信号,混在一起处理会出大问题。

## 关键接口

```python
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]   # JSON Schema
    handler: Callable[..., Any]  # 同步或异步都行

class ToolRegistry:
    def register(self, tool: Tool) -> None: ...
    def get(self, name: str) -> Tool | None: ...
    def specs(self) -> list[dict[str, Any]]: ...  # 喂给 model.request(..., tools=...)

@dataclass(frozen=True)
class ToolResult:
    output: str
    error: str | None = None

async def dispatch(registry: ToolRegistry, call: ToolCall, *, timeout_seconds=30.0) -> ToolResult: ...
```

## 现场要写的东西

- [ ] `Tool` / `ToolRegistry`(注册、按名字取出、拒绝重复注册、`specs()` 吐出 wire 格式的 schema 列表)。
- [ ] `dispatch()` 的三条分支:
    - **unknown tool**——Registry 里根本没有这个名字,handler 不能被调用。
    - **invalid arguments**——参数缺了 schema 里声明的必填字段,handler 也不能被调用。
    - **handler 抛异常 / 超时**——handler 真的跑了,但出了问题,要把异常接住,不能让它冒出 `dispatch`。
- [ ] 同步 handler 和异步 handler 统一适配——真实的文件/子进程调用大概率是阻塞的,不能直接摆在事件
  循环上跑。
- [ ] 三个具体工具:`read_file`、`list_files`、`edit_file`,都限制在项目目录以内。

## 试一试

不接模型,直接测 `dispatch()` 本身:

```bash
uv run scripts/check_tools.py
```

这个脚本也是给好的,依次跑三种情况——不存在的工具名、缺必填参数、真实的文件读取——每种情况后面
带一个断言,输出不对会直接报错,不用你自己盯着三行 print 判断对不对。

## 对照阅读:看看 Phi 怎么做

打开 `phi/src/phi/tools/dispatcher.py` 和 `phi/src/phi/tools/approval.py`。今天我们的 `dispatch()`
拿到工具就直接跑了,真实 Phi 在"校验参数"和"真正执行"之间,还插了一层:

- **审批系统**:每个工具都有一个 `approval_class`(只读/会改动工作区/不受限),不同的审批模式(默认、
  批准编辑、只读的 plan 模式……)决定这一类工具是自动放行、需要问一下用户、还是直接拒绝。这是"模型
  可以提议任何事,但危险的操作需要人点头"这条原则的具体实现。
- **可信参数注入**:有些参数(比如工作目录)不该由模型决定,而是 Harness 在工具真正执行前偷偷塞进
  去的,模型的 schema 里根本看不到这个参数。
- **动态 timeout**:模型可以在参数里请求一个更长的超时时间,Dispatcher 会做合理性检查再决定要不要用。

我们今天略过审批系统,不是因为它不重要——恰恰相反,这是让一个 Agent 敢在真实项目里跑起来的关键之一。
只是"要不要问用户""问哪些""怎么记住这次会话已经批准过",这些设计决策本身就够上一整节课的分量,先
放到概念课里讲。
