# Chapter 06 · Safety & Approval

<div class="phi-chapter-meta" markdown>
<span>概念讲解</span><span>不现场写代码</span><span>对照 phi/tools/types.py、phi/tools/dispatcher.py、phi/tools/approval.py、phi/tools/builtin/files.py、phi/tools/builtin/shell.py、phi/environment/confined.py</span>
</div>

mini-agent 里,模型一旦发出 Tool Call,工具立刻执行——中间没有任何人问一句"真的要这么做吗"。对
`read_file` 读你自己写的玩具项目,这没什么风险。但换成一个会写文件、会跑 shell 命令的 Agent,同样
的"模型说了就干"就完全不一样了:模型可能理解错了任务、可能被上下文里的某段文本诱导执行了不该执行
的操作,而 `rm -rf` 或者改错文件都不是能撤销的事。这一章讲的是:在"模型提出请求"和"操作真的发生"
之间,应该插入什么样的机制。

## 权限不能只有"有"和"没有"两档

真实场景里,工具的风险从来不是均匀分布的:读一个文件和删一个文件不是一回事,改工作区里的代码和执
行任意 shell 命令也不是一回事。如果只有"全部允许"或"全部拒绝"两个选项,要么你被烦到关掉所有确
认,要么 Agent 变得没法自主做完一个任务。真正要解决的问题是:怎么按风险分层,而不是按工具逐个手动
管理。

## 主流做法

- **命令白名单/黑名单**:只允许或禁止特定命令字符串,规则直白,但覆盖不到语义相近的变体。
- **沙箱/容器隔离**:整个执行环境跑在隔离的容器或虚拟机里,即使执行了危险操作,影响范围也被限制住。
- **人在回路中确认**:执行前对风险操作弹出确认,由人做最终判断——Claude Code 自己的模式切换就是
  这个思路的一种实现:只读的 Plan 模式,和放开手让它自由编辑的模式,是两档完全不同的默认权限。
- 上面几种通常是组合使用,而不是互斥选择。

## Phi 怎么做：追踪一条 Tool Call 何时获得执行权

这一节要沿源码回答一个具体问题：

> Model 生成了一条 `write` 或 `bash` Tool Call，从这段不可信输出到操作真正发生，中间经过哪些安全
> 边界？每一层分别能防住什么？

在 VS Code 中打开以下文件：

```text
phi/src/phi/tools/types.py
phi/src/phi/tools/dispatcher.py
phi/src/phi/tools/approval.py
phi/src/phi/environment/confined.py
phi/src/phi/tools/builtin/files.py
phi/src/phi/tools/builtin/shell.py
```

先建立完整的控制流：

```text
Model 提出的 Tool Call
        ↓
ToolRegistry 查找可信 Tool 定义
        ↓
严格校验 Model 提供的参数
        ↓
ApprovalPolicy：allow / deny / ask
        ↓ allow
注入可信的 FileSystem 或 Shell
        ↓
Tool handler
        ↓
Environment 执行实际副作用
        ↓
统一的 ToolResult
```

网页中的代码只摘录决定权限流向的关键骨架，完整实现以右侧源码为准。

这条链上有两类不同的保护：Approval 决定“这次是否授权”，Environment 决定“即使授权，能力最多能触及
哪里”。

### 第一步：从 Tool 的可信风险标签开始

在 `tools/types.py` 中找到 `ApprovalClass`：

```python
class ApprovalClass(StrEnum):
    READ_ONLY = "read_only"
    MUTATES_WORKSPACE = "mutates_workspace"
    UNCONFINED = "unconfined"
```

这个分类属于 `Tool` 定义，而不是 Model 生成的 `ToolCall`。Model 只能提供工具名和参数，无法声明自己
的调用是低风险操作。

接着打开 `builtin/files.py` 和 `builtin/shell.py`，对照三个例子：

```python
@tool(name="read", approval_class=ApprovalClass.READ_ONLY)
async def read_file(...): ...

@tool(name="write", approval_class=ApprovalClass.MUTATES_WORKSPACE)
async def write_file(...): ...

@tool(name="bash", approval_class=ApprovalClass.UNCONFINED)
async def run_bash(...): ...
```

`read_only`、`mutates_workspace` 和 `unconfined` 表示三个粗粒度能力等级。Phi 在这一层没有根据具体
命令字符串猜测风险；`bash` 始终属于 `unconfined`。

### 第二步：进入唯一的执行入口

打开 `tools/dispatcher.py`，找到 `ToolDispatcher.dispatch()`。一条 Tool Call 首先按名称查找注册表：

```python
tool = self._registry.get(call.name)
if tool is None:
    return ToolResult(
        call_id=call.id,
        output="",
        error=f"unknown_tool: {call.name}",
    )
```

这里取回的是运行时预先注册的可信 `Tool`，其中包含 handler、参数模型和 `approval_class`。接下来
`_validated_arguments()` 使用本地 Tool 的 Pydantic 模型严格解析参数：

```python
arguments = self._validated_arguments(tool, call.arguments)
```

在 `tools/types.py` 的 `_argument_model()` 中可以看到参数模型使用 `strict=True` 和 `extra="forbid"`。
Model 不能靠额外字段扩大调用面，也不能依赖模糊的隐式类型转换进入 handler。

执行顺序在这里很重要：工具存在且参数结构有效之后，Phi 才进入审批；审批通过之后，handler 才可能
运行。未知工具、无效参数和审批拒绝都会被转换成带原始 `call_id` 的 `ToolResult`，让 Agent Loop 能把
失败事实交还给 Model。

### 第三步：把风险分类映射成当前模式的规则

切到 `approval.py`，找到五个预设 `ApprovalMode`。每个模式把三档风险映射到 `ALLOW`、`DENY` 或
`ASK`：

| Mode | read_only | mutates_workspace | unconfined |
| --- | --- | --- | --- |
| `default` | allow | ask | ask |
| `accept_edits` | allow | allow | ask |
| `plan` | allow | deny | deny |
| `headless` | allow | deny | deny |
| `bypass` | allow | allow | allow |

现在进入 `RuleBasedApprovalPolicy.decide()`，按实际控制流阅读：

```python
matching = tuple(
    rule
    for rule in self.mode.rules
    if fnmatchcase(tool.name, rule.tool_pattern)
    and (
        rule.approval_class is None
        or rule.approval_class == tool.approval_class
    )
)
```

规则同时匹配工具名模式和风险分类。后续决策遵循三个安全约束：

1. 任意匹配的 `DENY` 优先，宽泛的 allow 不能覆盖更具体的拒绝；
2. 没有交互式 resolver 时，`ASK` 会变成 `DENY`，headless 路径 fail closed；
3. resolver 的普通异常会 fail closed，只有显式 `ALLOW_ONCE` 或 `ALLOW_FOR_SESSION` 才放行；
   `CancelledError` 仍作为 Run 的取消控制流向上传播。

`ALLOW_FOR_SESSION` 只把工具名写入当前 Policy 对象的内存集合：

```python
if resolution is AskResolution.ALLOW_FOR_SESSION:
    self._session_allowances.add(tool.name)
    return ApprovalDecision.ALLOW
```

它不会持久化为下一次进程的默认授权，也不会自动放行同一风险分类下的其他工具。

### 第四步：回到 dispatcher，找到副作用发生前的最后关口

审批结果回到 `ToolDispatcher.dispatch()`：

```python
decision = await policy.decide(call, tool)
if decision is ApprovalDecision.DENY:
    return ToolResult(
        call_id=call.id,
        output="",
        error=f"approval_denied: {tool.name}",
    )
```

只有 `ALLOW` 会继续向下。接下来 dispatcher 注入由运行时 wiring 提供的可信值：

```python
for parameter in tool.injected_parameters:
    if parameter not in self._trusted_values:
        raise RuntimeError(...)
    arguments[parameter] = self._trusted_values[parameter]
```

回看 `read_file()` 的签名：

```python
async def read_file(
    path: str,
    filesystem: Injected[FileSystem],
    ...,
):
```

`filesystem` 不出现在发给 Model 的 Tool schema 中，Model 也不能用同名参数替换它。这样，工作区根目录
与具体 Environment 实现由 `bootstrap.py` 中的可信 runtime wiring 组装，而不是由不可信 Tool Call
决定。

最后，dispatcher 为调用设置超时，捕获可恢复的 handler 错误，并把结果统一为 `ToolResult`。Run 的
取消仍然以 `CancelledError` 向上传播，因为取消属于 Harness 控制流，而不是一次普通工具失败。

### 第五步：沿文件路径检查结构性 confinement

打开 `environment/confined.py`，从 `ConfinedEnvironment` 看两个能力是怎样组装的：

```python
self.filesystem = ConfinedFileSystem(canonical_root, protected_patterns)
self.shell = WorkspaceShell(canonical_root)
```

先进入 `ConfinedFileSystem._candidate()`。它把相对路径接到工作区根目录，消除 `..`，并做第一次词法
边界检查：

```python
candidate = supplied if supplied.is_absolute() else self.root / supplied
lexical = Path(os.path.abspath(candidate))
lexical.relative_to(self.root)
```

词法检查还不能处理符号链接，所以已存在路径继续进入 `_resolve_existing()`，写入目标进入
`_resolve_write_target()`。两条路径最终都调用 `_validate_canonical()`：

```python
relative = canonical.relative_to(self.root)
if any(relative_path.match(pattern) for pattern in self.protected_patterns):
    return FileError(..., "path is protected")
```

因此 `../outside`、指向工作区外的 symlink，以及默认受保护的 `.git`、`.env*` 都无法被文件工具访问。
对于尚不存在的写入目标，Phi 会先解析并校验已经存在的父目录，再拼回文件名，避免通过父目录 symlink
逃逸。

### 第六步：明确 Approval 与 Confinement 的边界

最后阅读 `WorkspaceShell.exec()`。它把工作区设置为命令的 `cwd`：

```python
process = await asyncio.create_subprocess_shell(
    command,
    cwd=self.root,
    ...,
)
```

`cwd` 是命令的起始目录，不是路径 confinement。shell 命令仍然可以使用绝对路径、`..`、网络和当前
用户拥有的其他系统能力。`run_bash` 的 Tool 描述和风险标签都显式表达了这个事实：

```python
description=(
    "Run an unconfined shell command from the workspace working directory. "
    "This is not path-confined or operating-system sandboxed."
),
approval_class=ApprovalClass.UNCONFINED,
```

所以两层机制的边界是：

```text
ApprovalPolicy
    决定当前 Tool Call 是否可以开始执行

ConfinedFileSystem
    即使调用获准，也把文件能力限制在工作区和非保护路径

WorkspaceShell
    只有 cwd 与超时/取消清理，不提供 OS 级隔离
```

### 读完这条主线后

现在应该能够沿源码回答以下问题：

1. 为什么 `approval_class` 必须来自注册后的 `Tool`，而不能来自 Model 的 Tool Call？
2. 一条 Tool Call 在 handler 运行前依次经过哪些检查？
3. headless 环境遇到 `ASK` 时为什么会拒绝执行？
4. `Injected[FileSystem]` 怎样阻止 Model 选择或替换可信 Environment？
5. `ConfinedFileSystem` 如何阻止通过 `..`、工作区外绝对路径和 symlink 逃逸？
6. 为什么把工作区设为 shell 的 `cwd` 不等于把 shell 限制在工作区？

## 讨论

`WorkspaceShell` 不受路径限制这件事,如果不在文档里写清楚,而是留给用户自己去发现,会有什么后果?
一个"诚实地暴露边界"的设计和一个"假装什么都管住了"的设计,对使用者的实际安全水平有什么不同?如果
只能选一种,你会选哪一种,为什么?

??? success "展开参考答案"

    如果不说明 `WorkspaceShell` 不受路径限制，使用者很可能把“文件工具被限制在工作区”误解成
    “Agent 的所有操作都被限制在工作区”，进而在错误的安全假设下授予权限。

    诚实暴露边界不能直接消除风险，但能让使用者据此选择更严格的 approval mode、容器或操作系统级沙
    箱。相比之下，假装已经管住了会制造虚假的安全感，实际风险反而更高。因此应选择诚实暴露边界，同时
    尽可能通过默认拒绝、明确提示和外部隔离进一步缩小风险。
