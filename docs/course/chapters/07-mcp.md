# Chapter 07 · MCP

<div class="phi-chapter-meta" markdown>
<span>概念讲解</span><span>不现场写代码</span><span>对照 phi/mcp/client.py、phi/mcp/tools.py、phi/tools/registry.py、phi/tools/dispatcher.py</span>
</div>

mini-agent 的每一个工具都是手写在同一个代码库里的 Python 函数——工具集合等于你愿意写多少代码。这
在一个人的玩具项目里没问题,但如果你想用别人写好的工具呢?比如某个团队已经做好了一个能查内部知识
库的工具,你并不想为了用它而去读懂对方的 Python 代码、把它塞进你的项目里、还要保证两边的依赖不打
架。真正的问题是:两个独立的系统,怎么在不共享代码库、甚至不知道对方用什么语言写的情况下,让一个
系统的模型调用另一个系统暴露出来的工具。

## 工具集成要不要一个共同的协议

如果每一次集成都是"读对方文档,现写一段胶水代码",集成数量一多,维护成本会线性甚至更快地增长,而
且没有任何一段胶水代码可以复用到下一次集成上。要解决这个问题,需要的是一个双方都认的协议,而不是
一次性的手工对接。

## 主流做法

- **每次都手写一次性集成**:没有共享协议,每接入一个新工具源就重新写一遍对接逻辑,谈不上可复用。
- **采用 Model Context Protocol(MCP)**:一个开放协议(源自 Anthropic,现在被多种 Agent 工具采
  用),定义了模型侧的客户端如何发现和调用一个独立服务器进程暴露出来的工具、资源、提示词,双方只需
  要遵守同一份协议规范,不需要共享代码。

## Phi 怎么做：把远端 MCP Tool 接入同一条执行链

这一节要沿源码回答一个具体问题：

> 一个独立 MCP server 启动后，它发布的远端 Tool 怎样变成 Phi 中可调用的 Tool？调用过程中哪些逻辑
> 复用现有 Harness，哪些逻辑必须留在 MCP adapter？

在 VS Code 中打开以下文件：

```text
phi/src/phi/mcp/client.py
phi/src/phi/mcp/tools.py
phi/src/phi/tools/registry.py
phi/src/phi/tools/dispatcher.py
```

先建立从启动到调用的完整路径：

```text
McpConfig 中的一条 stdio server 配置
        ↓
启动子进程并完成 MCP initialize
        ↓
按 server capabilities 发现 Tools / Resources / Prompts
        ↓
校验发现结果
        ├── Tools → 构造 namespaced Phi Tool → 注册到公共 ToolRegistry
        │                                      ↓
        │                         Model 提出 mcp__{server_id}__{tool_name}
        │                                      ↓
        │                         公共 ToolDispatcher：审批、超时、错误归一化
        │                                      ↓
        │                         ClientSession.call_tool() → 脱敏后的 ToolResult
        ├── Resources → 缓存在 McpRuntime → 通过两个只读 meta-tools 暴露
        └── Prompts → 缓存在 McpRuntime → 由可信 Host 列出或选择
```

网页中的代码只摘录连接、适配和调用的关键骨架，完整实现以右侧源码为准。

### 第一步：从多个 server 的启动入口开始

在 `mcp/client.py` 中找到 `connect_mcp_servers()`。它按稳定的 server ID 顺序处理所有启用配置：

```python
for server_id in sorted(config.servers):
    server_config = config.servers[server_id]
    if not server_config.enabled:
        continue
```

每个 server 都经历三个连续阶段：连接、把发现到的远端 Tool 适配成 Phi Tool、整批注册。

```python
connection = await _connect_server(server_id, server_config, cwd)
registered_tools = tuple(
    build_remote_tool(...)
    for remote_tool in connection.tools
)
registry.register_many(registered_tools)
```

只有三个阶段全部成功，连接才进入 `McpRuntime`。如果其中一步失败，Phi 会关闭这个 server，记录
`McpDiagnostic` 和 `McpServerConnectFailed`，然后继续处理下一个 server。

这里保护的不变量是：一个 server 要么完整注册自己的全部 Tools，要么一个也不留下；单个 server 的
失败不会阻止后续健康 server 启动。

### 第二步：看 initialize 如何决定发现哪些能力

从 `_connect_server()` 进入 `_own_server()`。每个 server 由一个长期 owner task 持有 stdio transport、
`ClientSession` 和 `AsyncExitStack`：

```python
streams = await stack.enter_async_context(stdio_client(...))
session = await stack.enter_async_context(ClientSession(*streams))
initialized = await session.initialize()
```

握手完成后，Phi 只请求 server 在 initialize 响应中声明支持的能力：

```python
tools = (
    await _list_all_tools(session)
    if initialized.capabilities.tools is not None
    else ()
)
resources = (
    await _list_all_resources(session)
    if initialized.capabilities.resources is not None
    else ()
)
prompts = (
    await _list_all_prompts(session)
    if initialized.capabilities.prompts is not None
    else ()
)
```

三个 `_list_all_*()` 共享 `_list_all()` 的 cursor 循环，因此发现结果不只包含第一页。得到完整快照之后，
owner task 通过 `ready` Future 把 session 与能力交给启动方，然后等待 `close_requested`，让同一个任务
最终退出自己打开的所有异步上下文。

### 第三步：在注册前处理不可信发现元数据

继续看 `_validate_discovery_metadata()`。MCP server 发布的 Tool 名称、input schema、Resource 身份和
Prompt 参数都来自进程外部，Phi 会检查这些结构字段是否包含配置给该 server 的环境变量值：

```python
for remote_tool in tools:
    _reject_configured_value(remote_tool.name, configured_values, "Tool name")
    _reject_configured_value(
        remote_tool.inputSchema,
        configured_values,
        "Tool input schema",
    )
```

结构身份或 schema 一旦被 secret 污染，Phi 会隔离整个 server。原因是简单替换字符串可能改变工具名
或 schema 语义，产生一个含义不明确的 Tool。

普通 description、content 和 structuredContent 可以保持结构后递归脱敏；异常文本则会先脱敏，再压平
空白并截断。这个区分形成两种处理策略：

```text
Tool name / schema / Resource URI 等结构字段
    → 发现阶段检测到 secret 就 fail closed

description / content / structuredContent
    → 在跨出 MCP adapter 前递归替换为 [redacted]

exception text
    → 脱敏后压平并截断为安全摘要
```

### 第四步：把远端定义适配成公共 Tool

切到 `mcp/tools.py`，找到 `build_remote_tool()`。命名空间先把 server 身份编码进工具名：

```python
tool_name = f"mcp__{server_id}__{remote_tool.name}"
```

这既避免不同 server 的同名 Tool 冲突，也让 Trace、审批界面和 Tool Result 能指出调用来源。

函数最终返回 Phi 的公共 `Tool`：

```python
return Tool(
    name=tool_name,
    description=redact_mcp_data(...),
    handler=call,
    args_schema=remote_tool.inputSchema,
    args_model=None,
    approval_class=ApprovalClass.UNCONFINED,
)
```

这里有两个关键决定：

- 保留 server 发布的原始 input schema；`args_model=None` 表示具体参数语义仍由远端验证；
- 所有远端 MCP Tool 都标记为 `UNCONFINED`，因为 Phi 不能证明另一个进程内部的能力边界。

适配后的 Tool 进入与 `read`、`write`、`bash` 相同的 `ToolRegistry`。Context 构建和 Agent Loop 不需要
知道某个 schema 来自本地函数还是 MCP server。

### 第五步：沿一次远端调用走完公共 dispatcher

当 Model 提出 `mcp__demo__echo` 时，调用先进入 `ToolDispatcher.dispatch()`。因此第 06 章中的审批、
超时、取消和 `ToolResult` 规则全部复用：

```text
ToolRegistry 查找
→ ApprovalPolicy（MCP Tool 是 unconfined）
→ dispatcher timeout
→ MCP Tool handler
```

交互式 Host 中的 `default` 模式会向用户询问；没有 resolver 时，`ASK` 会变成拒绝；`plan` 和
`headless` 模式则直接拒绝。只有审批策略明确允许后，handler 才调用绑定的远端函数。

在 `mcp/client.py` 中进入 `_bind_remote_call()`，可以看到真正的协议调用：

```python
async def call(arguments: dict[str, Any]) -> types.CallToolResult:
    return await session.call_tool(tool_name, arguments)
```

再回到 `mcp/tools.py` 中 `build_remote_tool()` 内部的 `call(**arguments)`，观察 adapter 怎样收束远端
结果：

```python
try:
    result = await call_remote(arguments)
except asyncio.CancelledError:
    raise
except Exception as error:
    return ToolFailure(f"{tool_name}: {safe_error_summary(error, secrets)}")
```

transport 或协议异常成为可恢复的 `ToolFailure`；取消继续向 Harness 传播。MCP 的 `isError=True` 也成为
`ToolFailure`，其中脱敏后的错误信封会被序列化进错误文本。成功结果同时保留 `content` 和可选的
`structuredContent`，再由公共 dispatcher 序列化为 Model 可消费的文本。

### 第六步：确认数据在哪些出口完成脱敏

在 `mcp/tools.py` 中找到 `_tool_result_envelope()`：

```python
envelope = {
    "content": [
        item.model_dump(mode="json", by_alias=True, exclude_none=True)
        for item in result.content
    ]
}
if result.structuredContent is not None:
    envelope["structuredContent"] = result.structuredContent

return redact_mcp_data(envelope, secrets)
```

`redact_mcp_data()` 会递归处理字符串、映射的键和值以及序列。`safe_error_summary()` 也先替换 secret，再
压平空白并限制长度。相同的处理还应用于 Resource 内容、Prompt 内容、诊断和 Event，避免配置环境值从
另一条输出路径泄露。

这层脱敏降低的是“已配置 secret 被 server 回显”的风险。MCP 返回的其他内容仍然是不可信输入；协议
兼容并不等价于服务器可信。

### 第七步：最后读连接怎样被可靠关闭

回到 `client.py`，查看 `_ServerConnection.close()`：

```python
self.close_requested.set()
await asyncio.shield(self.owner_task)
```

关闭请求唤醒 `_own_server()`，使 `AsyncExitStack` 在创建资源的 owner task 内按逆序退出。`shield` 保证
调用方被取消时，transport 和子进程仍先完成回收，再传播取消。

`McpRuntime.close()` 按连接建立的逆序关闭全部 server，并且可以重复调用：

```python
if self._closed:
    return
self._closed = True

for connection in reversed(tuple(self._connections.values())):
    await connection.close()
```

启动流程整体被取消时，`connect_mcp_servers()` 也会关闭此前已经连接的 server。这样，server 的启动
失败、Host 正常退出和任务取消最终都汇入同一条资源回收路径。

### 读完这条主线后

现在应该能够沿源码回答以下问题：

1. 一个 MCP server 在什么时刻才算成功进入 `McpRuntime`？
2. Phi 为什么只发现 initialize 响应中明确声明的 capabilities？
3. 为什么结构元数据中的 secret 要隔离整个 server，而普通内容可以脱敏后继续使用？
4. 远端 MCP Tool 进入 `ToolRegistry` 后复用了哪些现有 Harness 边界？
5. 为什么所有远端 MCP Tool 都被标记为 `UNCONFINED`？
6. transport 异常、MCP `isError` 和任务取消分别怎样离开 adapter？
7. owner task 与 `AsyncExitStack` 怎样保证 stdio 子进程最终被回收？

## 讨论

"一个坏掉的 MCP 服务器不该拖垮其他服务器"这个设计目标,如果反过来想——一个 MCP 服务器返回的内容
本身是不可信的(它可能被入侵,或者本身就是恶意实现),仅靠"发现失败要隔离"和"密钥要脱敏"这两条,
够不够构成一个完整的信任边界?你觉得还缺什么?

??? success "展开参考答案"

    不够。“发现失败要隔离”主要保护可用性，“密钥脱敏”只防住了一部分敏感信息泄漏；它们没有限制恶
    意服务器真正能够做什么。

    完整的信任边界还需要考虑服务器身份与配置来源、允许暴露哪些工具、参数和返回值校验、超时与资源限
    制、进程和网络隔离、危险调用的用户批准，以及审计和撤销机制。服务器返回的描述和内容也可能包含
    prompt injection，因此不能因为它符合 MCP 协议就把它当成可信指令。
