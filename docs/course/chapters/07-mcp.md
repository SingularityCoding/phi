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

## Phi 怎么做:把远端 MCP Tool 接入同一条执行链

这一节要沿源码回答一个具体问题:

> 一个独立 MCP server 启动后,它发布的远端 Tool 怎样变成 Phi 中可调用的 Tool?调用过程中哪些逻辑
> 复用现有 Harness,哪些逻辑必须留在 MCP adapter?

在 VS Code 中打开以下文件:

```text
phi/src/phi/mcp/client.py
phi/src/phi/mcp/tools.py
phi/src/phi/tools/registry.py
phi/src/phi/tools/dispatcher.py
```

完整路径:

```text
McpConfig 中的一条 stdio server 配置
        ↓
启动子进程并完成 MCP initialize、发现 Tools/Resources/Prompts
        ↓
Tools → 构造 namespaced Phi Tool → 注册到公共 ToolRegistry
        ↓
Model 提出 mcp__{server_id}__{tool_name}
        ↓
公共 ToolDispatcher:审批、超时、错误归一化(第 06 章讲过的那一条链)
        ↓
ClientSession.call_tool() → 脱敏后的 ToolResult
```

这一章真正值得课堂时间的只有一件事:**远端 Tool 一旦注册,就完全走公共 dispatcher,没有第二条执
行路径**。握手协议、分页发现、异步资源回收这些是 MCP 协议本身和通用 asyncio 工程的细节,整理成两
段可折叠的深入阅读,内容仍然直接嵌在这一页里。

在此之前先看一条结构性不变量:`connect_mcp_servers()` 按稳定顺序处理每个 server,任何一步失败都
不会让半注册的 server 进入运行时:

```python
for server_id in sorted(config.servers):
    ...
    try:
        connection = await _connect_server(server_id, server_config, cwd)
        registered_tools = tuple(build_remote_tool(...) for remote_tool in connection.tools)
        registry.register_many(registered_tools)
    except Exception as error:
        if "connection" in locals():
            await connection.close()
        diagnostics.append(McpDiagnostic(server_id, summary))
        continue
```

一个 server 要么连接、发现、整批注册全部成功,要么完全不出现在 `McpRuntime` 里;单个 server 失败
不阻止其他 server 启动。

### 第一步:注册前先检查发现元数据里有没有藏着 secret

MCP server 发布的 Tool 名称、input schema、Resource 身份都来自进程外部,是不可信输入。在
`client.py` 的 `_validate_discovery_metadata()` 里:

```python
for remote_tool in tools:
    _reject_configured_value(remote_tool.name, configured_values, "Tool name")
    _reject_configured_value(remote_tool.inputSchema, configured_values, "Tool input schema")
```

结构身份或 schema 一旦被配置的 secret 值污染,Phi 直接让这次连接失败,隔离整个 server——因为简单
替换字符串可能改变工具名或 schema 语义,产生一个含义不明确的 Tool。普通 description、content 走
的是另一条更宽松的路径(见第四步):可以脱敏后继续用,不需要拒绝整个 server。两者的区别是:**结
构字段错了会产生歧义,内容字段错了只是包含了不该出现的文本**。

### 第二步:namespacing 与 UNCONFINED——把远端 Tool 适配成公共 Tool

切到 `mcp/tools.py`,看 `build_remote_tool()`:

```python
tool_name = f"mcp__{server_id}__{remote_tool.name}"
...
return Tool(
    name=tool_name,
    description=redact_mcp_data(...),
    handler=call,
    args_schema=remote_tool.inputSchema,
    args_model=None,
    approval_class=ApprovalClass.UNCONFINED,
)
```

`mcp__{server_id}__{tool_name}` 避免不同 server 的同名 Tool 冲突,也让 Trace、审批界面和 Tool
Result 能指出调用来源。两个更关键的决定:`args_model=None` 表示参数语义仍由远端验证,Phi 不重新
声明一份本地 schema;所有远端 MCP Tool 都标记 `UNCONFINED`,因为 **Phi 不能证明另一个进程内部的
能力边界**——哪怕这个远端 Tool 实际只做了一次只读查询,Phi 也没有办法验证这件事。

### 第三步:远端调用复用的是同一个 dispatcher,不是另一套执行逻辑

这是本章真正的论点。`build_remote_tool()` 返回的 `Tool` 和 `read`、`write`、`bash` 进入的是同一
个 `ToolRegistry`。当 Model 提出 `mcp__demo__echo`,调用一样先进入
`ToolDispatcher.dispatch()`(第 06 章 `tools/dispatcher.py:54` 讲过的那条链):

```text
ToolRegistry 查找
→ ApprovalPolicy(MCP Tool 是 UNCONFINED,default/plan/headless 模式下会 ask 或 deny)
→ dispatcher timeout
→ MCP Tool 的 handler
```

只有审批通过,`build_remote_tool()` 内部的 `call()` 才会真正调用远端:

```python
async def call(**arguments: Any) -> dict[str, Any] | ToolFailure:
    try:
        result = await call_remote(arguments)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return ToolFailure(f"{tool_name}: {safe_error_summary(error, secrets)}")
    envelope = _tool_result_envelope(result, secrets)
    if result.isError:
        return ToolFailure(f"{tool_name}: server_error: {...}")
    return envelope
```

transport 或协议异常变成 `ToolFailure`,和一个本地 handler 抛出普通异常没有区别;`isError=True`
同样变成 `ToolFailure`;只有 `CancelledError` 继续向上传播,因为取消属于 Harness 的控制流。Context
构建和 Agent Loop 完全不需要知道某个 Tool 的 schema 来自本地函数还是远端 MCP server。

### 第四步:内容字段在跨出 adapter 前统一脱敏

`_tool_result_envelope()` 是脱敏真正发生的地方:

```python
envelope = {"content": [...]}
if result.structuredContent is not None:
    envelope["structuredContent"] = result.structuredContent
return redact_mcp_data(envelope, secrets)
```

`redact_mcp_data()` 递归替换字符串、映射键值和序列中出现的已配置 secret;`safe_error_summary()`
对异常文本做同样处理。这层脱敏降低的是"已配置 secret 被 server 回显"的风险——**协议兼容不等于服
务器可信**,MCP 返回的其他内容仍然是不可信输入,这也是本章讨论题要问的问题。

??? note "深入阅读(课后):MCP 握手与分页发现"

    每个 server 由一个长期 owner task 持有 stdio transport、`ClientSession` 和
    `AsyncExitStack`:

    ```python
    streams = await stack.enter_async_context(stdio_client(...))
    session = await stack.enter_async_context(ClientSession(*streams))
    initialized = await session.initialize()
    ```

    握手完成后,Phi **只**请求 server 在 `initialize` 响应中声明支持的能力:

    ```python
    tools = (
        await _list_all_tools(session)
        if initialized.capabilities.tools is not None
        else ()
    )
    ```

    `resources`、`prompts` 走同样的判断。三者共享 `_list_all()` 的游标循环,确保发现结果不只是
    第一页:

    ```python
    async def _list_all(fetch_page, select_items):
        items = []
        cursor = None
        while True:
            result = await fetch_page(cursor)
            items.extend(select_items(result))
            cursor = result.nextCursor
            if cursor is None:
                return tuple(items)
    ```

    完整实现见 `phi/src/phi/mcp/client.py` 中的 `_own_server()` 和 `_list_all()`。

??? note "深入阅读(课后):连接的显式异步生命周期回收"

    `_ServerConnection.close()` 用 `shield` 保护 owner task 的退出过程:

    ```python
    self.close_requested.set()
    try:
        await asyncio.shield(self.owner_task)
    except asyncio.CancelledError:
        while not self.owner_task.done():
            try:
                await asyncio.shield(self.owner_task)
            except asyncio.CancelledError:
                continue
        raise
    ```

    关闭请求唤醒 `_own_server()`,让 `AsyncExitStack` 在创建资源的同一个任务内按逆序退出;即使调
    用方被取消,transport 和子进程也会先完成回收,取消再向上传播。`McpRuntime.close()` 则以连接
    建立的逆序关闭全部 server,并且可重复调用:

    ```python
    for connection in reversed(tuple(self._connections.values())):
        await connection.close()
    ```

    server 启动失败、Host 正常退出、任务取消,最终都汇入这同一条资源回收路径。完整实现见
    `phi/src/phi/mcp/client.py` 中的 `_ServerConnection.close()` 和 `McpRuntime.close()`。

### 读完这条主线后

现在应该能够沿源码回答以下问题:

1. 一个 MCP server 要么完整注册全部 Tools、要么一个都不留,这个不变量在哪段代码里保证?
2. 为什么结构元数据(Tool 名称、schema)里出现 secret 要隔离整个 server,而普通 content 只需要
   脱敏?
3. 为什么所有远端 MCP Tool 都被标记为 `UNCONFINED`?
4. 远端 MCP Tool 进入 `ToolRegistry` 后,复用了第 06 章的哪些边界?
5. transport 异常、MCP 的 `isError` 和任务取消,分别怎样离开 adapter?
6.(对应深入阅读)owner task 与 `AsyncExitStack` 怎样保证 stdio 子进程最终被回收?

## 讨论

"一个坏掉的 MCP 服务器不该拖垮其他服务器"这个设计目标,如果反过来想——一个 MCP 服务器返回的内容
本身是不可信的(它可能被入侵,或者本身就是恶意实现),仅靠"发现失败要隔离"和"密钥要脱敏"这两条,
够不够构成一个完整的信任边界?你觉得还缺什么?

??? success "展开参考答案"

    不够。"发现失败要隔离"主要保护可用性,"密钥脱敏"只防住了一部分敏感信息泄漏;它们没有限制恶
    意服务器真正能够做什么。

    完整的信任边界还需要考虑服务器身份与配置来源、允许暴露哪些工具、参数和返回值校验、超时与资源限
    制、进程和网络隔离、危险调用的用户批准,以及审计和撤销机制。服务器返回的描述和内容也可能包含
    prompt injection,因此不能因为它符合 MCP 协议就把它当成可信指令。
