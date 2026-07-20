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

## Phi 怎么做：沿着一次 Tool Call 穿过执行边界

这一章的源码主线是 Model 提议的一次调用：

> 从 `ToolCall(name, arguments)` 到 `ToolResult`，Phi 在真正运行 handler 之前做了哪些判断？

在 VS Code 中打开以下文件：

```text
phi/src/phi/tools/types.py
phi/src/phi/tools/registry.py
phi/src/phi/tools/dispatcher.py
phi/src/phi/tools/approval.py
```

一次完整的 Tool Call 会经过以下路径：

```text
Tool definition
    ├─→ ToolRegistry.specs() ─→ Model 可见的 schema
    └─→ ToolRegistry.get()
              ↓
       validate arguments
              ↓
       decide approval
              ↓
       inject trusted values
              ↓
       timeout + invoke handler
              ↓
          ToolResult
```

这条路径上只有 `ToolDispatcher.dispatch()` 有权从 Model 的“提议”跨到真实执行。

### 第一步：从一个 handler 生成两种表示

在 `tools/types.py` 中搜索 `tool()` 和 `_argument_model()`。

Phi 用装饰器把一个有完整类型注解的 Python handler 转换成 `Tool`：

```python
@tool(
    name="read",
    description="Read a bounded range of lines from a workspace text file.",
    approval_class=ApprovalClass.READ_ONLY,
)
async def read_file(
    path: str,
    filesystem: Injected[FileSystem],
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(gt=0)] = 200,
) -> str | ToolFailure:
    ...
```

`_argument_model()` 读取函数签名，并同时产生：

- 用于生成 JSON Schema 的 Pydantic `args_model`；
- 由可信运行时提供、不能暴露给 Model 的 `injected_parameters`。

参数模型采用严格配置：

```python
model = create_model(
    model_name,
    __config__=ConfigDict(extra="forbid", strict=True),
    **fields,
)
```

因此字符串 `"3"` 不会自动转成整数 `3`，额外参数也不会被悄悄传给 handler。`Injected[T]` 标记的参数
则根本不会进入这个 Model 可见模型。

接着查看 `Tool.__post_init__()`。Tool 名称必须满足 OpenAI-compatible 命名规则，描述不能为空，timeout
必须是有限正数；`args_schema` 还会被递归冻结。这里建立的第一个不变量是：

> Tool 注册完成后，它的名称、权限类别和 Model 可见 schema 都不能在背后漂移。

### 第二步：看 Registry 怎样决定 Model 能看到什么

切换到 `tools/registry.py`，搜索 `ToolRegistry.specs()`。

```python
return [
    {
        "type": "function",
        "function": {
            "name": registered_tool.name,
            "description": registered_tool.description,
            "parameters": _mutable_json(registered_tool.args_schema),
        },
    }
    for registered_tool in sorted(self._tools.values(), key=lambda item: item.name)
]
```

Registry 同时承担两项职责：按唯一名称保存可信 `Tool`，以及生成发送给 Model 的 schema。`specs()` 按名称
排序，使同一组 Tool 产生稳定的请求内容；返回值还是一份可变副本，调用方修改 wire dictionary 不会污染
Registry 中被冻结的 schema。

在 `tests/tools/test_tool_registry.py` 中找到 `test_specs_expose_only_model_controlled_arguments`。这个测试
展示了同一个 handler 的两类参数：`text` 和 `limit` 出现在 schema 中，`context: Injected[str]` 不出现。
Model 甚至不知道 `context` 存在，自然也没有机会伪造它。

### 第三步：从未知名称和非法参数开始阅读 Dispatcher

打开 `tools/dispatcher.py`，从 `ToolDispatcher.dispatch()` 顶部向下阅读。

第一道判断是按 Model 提供的名称查找 Tool：

```python
tool = self._registry.get(call.name)
if tool is None:
    return ToolResult(
        call_id=call.id,
        output="",
        error=f"unknown_tool: {call.name}",
    )
```

未知 Tool 是一次可恢复的 Tool 失败，不是 Python 异常。Harness 会把这条 `ToolResult` 送回 Model，让它
有机会改正名称。

找到 Tool 后，`_validated_arguments()` 使用刚才缓存的 Pydantic model 做严格校验。失败同样被归一化为
`invalid_arguments`，handler 不会运行：

```python
except ValidationError as exc:
    ...
    return ToolResult(..., error=f"invalid_arguments: {details}")
```

本地 Tool 有 `args_model`；MCP 等远端 Tool 可以只有 schema，此时远端协议仍拥有自己的验证语义。两者
共用 Dispatcher，但不会假装本地 Pydantic 已经验证了远端实现。

### 第四步：在执行前解析审批策略

参数合法后，Dispatcher 才调用 `ApprovalPolicy.decide()`：

```python
decision = await policy.decide(call, tool)
if decision is ApprovalDecision.DENY:
    return ToolResult(..., error=f"approval_denied: {tool.name}")
```

切换到 `tools/approval.py`，先查看 `ApprovalClass` 对应的三类权限：只读、修改工作区、不受路径限制。再
查看 `DEFAULT_MODE`、`PLAN_MODE` 和 `BYPASS_MODE`，观察同一种 Tool 在不同运行模式下如何得到不同的
默认决定。

继续搜索 `RuleBasedApprovalPolicy.decide()`。规则解析遵循以下顺序：

```text
匹配到任何 DENY
→ 当前 Session 是否已允许这个 Tool 名称
→ 第一条匹配的 ALLOW / ASK
→ 未匹配时的安全默认值
```

没有交互式 resolver 时，`ASK` 会变成 `DENY`；resolver 自身抛出异常时也不会授予权限。审批边界由此
满足 fail closed：无法确认允许，就不执行。

Dispatcher 可以调用 `approval_observer` 记录已经作出的决定，但 observer 的返回值不会参与决策。Event
可以观察行为，不能改变行为。

### 第五步：审批通过后再注入可信参数

回到 `ToolDispatcher.dispatch()`，找到 `tool.injected_parameters` 循环：

```python
for parameter in tool.injected_parameters:
    if parameter not in self._trusted_values:
        raise RuntimeError(...)
    arguments[parameter] = self._trusted_values[parameter]
```

这一步发生在 Model 参数校验和审批之后、handler 执行之前。以内置文件 Tool 使用的 `filesystem` 为
例，它来自创建 Dispatcher 时的运行时 wiring，而不是来自 Tool Call。

如果某个注入值缺失，Phi 会抛出 `RuntimeError`，让 Run 以内部配置缺陷结束，而不是把缺失值伪装成普通
Tool 错误。因为此时失败的不是 Model 提议，而是系统没有兑现自己声明的可信执行环境。

### 第六步：统一异步、同步、超时与返回值

通过前面所有判断后，Dispatcher 才进入 `_invoke()`：

```python
if _is_async_callable(tool.handler):
    return await tool.handler(**arguments)
output = await asyncio.to_thread(tool.handler, **arguments)
if inspect.isawaitable(output):
    return await output
```

同步 handler 被送入工作线程，避免文件或进程操作阻塞 Harness 的事件循环。Tool 自己可以声明固定
timeout；也可以指定一个 Model 可见的 timeout 参数。Dispatcher 会校验请求值为有限正数，并将实际
上限设为基础 timeout 与“请求值加一秒”中的较大者。额外的一秒用于取消后的清理。

执行结果最后被归一化：

| 情况 | `ToolResult` |
| --- | --- |
| 超时 | `tool_timeout: ...` |
| handler 抛出普通异常 | `handler_error: ...` |
| handler 返回 `ToolFailure` | 使用其中的预期错误文本 |
| handler 成功 | 输出稳定序列化为字符串 |

`asyncio.CancelledError` 是例外：它必须继续向上传播，因为它表示整个 Run 的控制流被取消，不能伪装成
一次可恢复的 handler 错误。

### 第七步：区分可恢复失败与边界缺陷

读完 `dispatch()` 后，可以把所有出口分成两类：

```text
Model 或 Tool 的预期失败
→ unknown / invalid / denied / timeout / handler_error
→ 返回 ToolResult
→ Model 下一步可以观察并恢复

Phi 自己的契约缺陷
→ 缺少可信注入值、审批 observer 故障等
→ 异常越过 Dispatcher
→ Harness 将 Run 标记为 FAILED
```

这个区分解释了为什么 Dispatcher 既不会让所有异常都崩掉 Run，也不会把所有问题都压成字符串。只有
Model 有机会根据反馈修正的失败，才应该留在 Tool 往返内部。

### 读完这条主线后

现在应该能够沿源码回答以下问题：

1. 一个 Python handler 怎样同时产生 Model 可见 schema 和本地严格参数模型？
2. 为什么 `Injected` 参数既不能出现在 schema 中，也不能接受同名 Model 参数覆盖？
3. 未知 Tool、非法参数和审批拒绝为什么都返回 `ToolResult`，而不是让 Run 直接失败？
4. Approval Policy 和 approval observer 分别能做什么，为什么 Event 观察者不能改变决定？
5. 同步 handler 为什么要通过 `asyncio.to_thread()` 执行？
6. 哪些错误属于可恢复的 Tool 失败，哪些错误说明 Phi 自己的执行边界已经失效？
