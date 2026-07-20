# Chapter 06 · Safety & Approval

<div class="phi-chapter-meta" markdown>
<span>概念讲解</span><span>不现场写代码</span><span>对照 phi/tools/types.py、phi/tools/approval.py、phi/tools/dispatcher.py、phi/tools/builtin/files.py、phi/tools/builtin/shell.py、phi/environment/confined.py</span>
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

## Phi 怎么做:追踪一条 Tool Call 何时获得执行权

这一节要沿源码回答一个具体问题:

> Model 生成了一条 `write` 或 `bash` Tool Call,从这段不可信输出到操作真正发生,中间经过哪些安全
> 边界?每一层分别能防住什么?

在 VS Code 中打开以下文件(阅读顺序和下面的步骤一致):

```text
phi/src/phi/tools/types.py
phi/src/phi/tools/dispatcher.py
phi/src/phi/tools/approval.py
phi/src/phi/tools/builtin/files.py
phi/src/phi/tools/builtin/shell.py
phi/src/phi/environment/confined.py
```

完整控制流:

```text
Model 提出的 Tool Call
        ↓
ToolRegistry 查找可信 Tool 定义
        ↓
严格校验 Model 提供的参数
        ↓
ApprovalPolicy:allow / deny / ask
        ↓ allow
注入可信的 FileSystem 或 Shell
        ↓
Tool handler
        ↓
Environment 执行实际副作用
        ↓
统一的 ToolResult
```

这条链上有两类不同的保护:Approval 决定"这次是否授权",Environment 决定"即使授权,能力最多能触及
哪里"。四个主步骤讲透这两层保护的骨架;路径 confinement 的具体解析算法整理成一段可折叠的深入阅
读,不占课堂时间,但内容仍然直接嵌在这一页里,不需要再切回编辑器。

### 第一步:风险标签来自 Tool 定义,不是来自 Model

在 `tools/types.py` 中找到 `ApprovalClass`:

```python
class ApprovalClass(StrEnum):
    READ_ONLY = "read_only"
    MUTATES_WORKSPACE = "mutates_workspace"
    UNCONFINED = "unconfined"
```

它是 `Tool` 这个不可变 dataclass 的一个字段:

```python
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Callable[..., Any]
    args_schema: Mapping[str, Any]
    args_model: type[BaseModel] | None = None
    approval_class: ApprovalClass = ApprovalClass.READ_ONLY
    ...
```

再看 `builtin/files.py` 和 `builtin/shell.py` 里三个例子:

```python
@tool(name="read", ..., approval_class=ApprovalClass.READ_ONLY)
async def read_file(...): ...

@tool(name="write", ..., approval_class=ApprovalClass.MUTATES_WORKSPACE)
async def write_file(...): ...

@tool(name="bash", ..., approval_class=ApprovalClass.UNCONFINED)
async def run_bash(...): ...
```

风险等级在 Tool 注册时就已经固定。Model 生成的 `ToolCall` 只有工具名和参数,没有任何字段能让它
自称"这次调用是低风险的"。

### 第二步:唯一执行入口——先查注册表,再做严格参数校验

打开 `tools/dispatcher.py`,看 `ToolDispatcher.dispatch()` 的开头:

```python
tool = self._registry.get(call.name)
if tool is None:
    return ToolResult(call_id=call.id, output="", error=f"unknown_tool: {call.name}")

try:
    arguments = self._validated_arguments(tool, call.arguments)
except ValidationError as exc:
    ...
    return ToolResult(call_id=call.id, output="", error=f"invalid_arguments: {details}")
```

`_validated_arguments()` 用的是 `types.py` 里 `_argument_model()` 生成的模型,关键在这一行:

```python
model = create_model(
    model_name,
    __config__=ConfigDict(extra="forbid", strict=True),
    **fields,
)
```

`strict=True` 关掉隐式类型转换,`extra="forbid"` 不允许额外字段——Model 不能靠塞一个没声明过的
参数,或者靠一次模糊的类型转换,扩大它能触达的执行面。未知工具和无效参数在这一步就已经变成带
`call_id` 的 `ToolResult`,还没有进入审批。

### 第三步:审批模式把三档风险映射成 allow / deny / ask,并且默认 fail closed

这是本章最重要的一步。切到 `approval.py`,先看五个预设模式:

| Mode | read_only | mutates_workspace | unconfined |
| --- | --- | --- | --- |
| `default` | allow | ask | ask |
| `accept_edits` | allow | allow | ask |
| `plan` | allow | deny | deny |
| `headless` | allow | deny | deny |
| `bypass` | allow | allow | allow |

再看真正做决定的 `RuleBasedApprovalPolicy.decide()`:

```python
matching = tuple(
    rule
    for rule in self.mode.rules
    if fnmatchcase(tool.name, rule.tool_pattern)
    and (rule.approval_class is None or rule.approval_class == tool.approval_class)
)
# 拒绝优先防止宽泛的 allow 规则意外覆盖更具体的 deny 规则。
if any(rule.decision is RuleDecision.DENY for rule in matching):
    return ApprovalDecision.DENY
# "本 Session 允许"只按 Tool 名称记忆,且永不写入持久化配置。
if tool.name in self._session_allowances:
    return ApprovalDecision.ALLOW

decision = matching[0].decision if matching else self.mode.on_unmatched
if decision is RuleDecision.ALLOW:
    return ApprovalDecision.ALLOW
if decision is RuleDecision.DENY or self._resolver is None:
    # 无交互 resolver 的 headless 路径必须 fail closed。
    return ApprovalDecision.DENY

try:
    resolution = await self._resolver(call, tool)
except asyncio.CancelledError:
    raise
except Exception:
    # UI resolver 故障时不授予执行权限。
    return ApprovalDecision.DENY
```

三条安全约束都在这段代码里:**任意匹配的 DENY 优先**;**没有交互式 resolver 时,ASK 会变成
DENY**(所以 headless 路径永远 fail closed);**resolver 抛出普通异常也 fail closed**,只有
resolver 明确返回 `ALLOW_ONCE` 或 `ALLOW_FOR_SESSION` 才放行,而 `CancelledError` 会继续向上传
播,因为取消是 Run 的控制流,不是一次审批结果。`ALLOW_FOR_SESSION` 只把工具名写进当前 Policy 对
象的内存集合,不会持久化,也不会自动放行同一风险分类下的其他工具。

### 第四步:dispatcher 是可信依赖唯一的注入点

审批通过之后,回到 `dispatch()` 里紧接着的这一段:

```python
for parameter in tool.injected_parameters:
    if parameter not in self._trusted_values:
        raise RuntimeError(...)
    arguments[parameter] = self._trusted_values[parameter]
```

对照 `types.py` 里的 `Injected`:

```python
class Injected:
    def __class_getitem__(cls, item: Any) -> Any:
        return Annotated[item, _INJECTED]
```

以及 `read_file()` 的签名:

```python
async def read_file(
    path: str,
    filesystem: Injected[FileSystem],
    ...,
):
```

`filesystem` 这个参数不会出现在发给 Model 的 Tool schema 里,Model 也不能用同名参数把它替换掉。
工作区根目录和具体 Environment 实现,由运行时 wiring 组装并交给 dispatcher,不是由不可信的 Tool
Call 决定。

??? note "深入阅读(课后):路径 confinement 怎么对付符号链接"

    上面四步讲的是"这次调用能不能开始执行"。真正拿到 `filesystem` 之后,`ConfinedFileSystem` 还
    要保证"即使调用获准,文件能力也只能触达工作区内的非保护路径"。这部分是通用的路径安全工程手
    法,不是 Agent 特有的概念,值得知道但不用占课堂时间。

    先做纯词法检查,这一步还不能处理符号链接:

    ```python
    def _candidate(self, path: str) -> Path | FileError:
        supplied = Path(path)
        candidate = supplied if supplied.is_absolute() else self.root / supplied
        # abspath 消除 `..`,但不会解析符号链接,因此这里只能阻止词法越界。
        lexical = Path(os.path.abspath(candidate))
        lexical.relative_to(self.root)
        ...
        return lexical
    ```

    已存在路径继续解析真实符号链接目标,写入路径则先验证已存在的父目录、再拼回文件名(避免通过
    父目录符号链接逃逸),两条路径最终都汇合到同一个规范化校验:

    ```python
    def _validate_canonical(self, canonical: Path, supplied_path: str) -> FileError | None:
        relative = canonical.relative_to(self.root)
        relative_path = PurePosixPath(relative.as_posix())
        if relative_path != PurePosixPath(".") and any(
            relative_path.match(pattern) for pattern in self.protected_patterns
        ):
            return FileError(FileErrorCode.PERMISSION_DENIED, supplied_path, "path is protected")
        return None
    ```

    `.git`、`.env*` 这类默认受保护路径,和指向工作区外的符号链接,都在这一步被拒绝。完整实现见
    `phi/src/phi/environment/confined.py` 中的 `ConfinedFileSystem`。

### 第五步:把工作区设为 shell 的 cwd,不等于把 shell 限制在工作区

最后看 `WorkspaceShell.exec()`:

```python
process = await asyncio.create_subprocess_shell(
    command,
    cwd=self.root,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    start_new_session=os.name == "posix",
)
```

`cwd` 只是命令的起始目录,不是路径 confinement。shell 命令仍然可以用绝对路径、`..`、网络和当前用
户拥有的其他系统能力。`run_bash` 的 Tool 描述和风险标签都显式承认了这一点:

```python
description=(
    "Run an unconfined shell command from the workspace working directory. "
    "This is not path-confined or operating-system sandboxed."
),
approval_class=ApprovalClass.UNCONFINED,
```

三层机制的边界现在可以完整拼出来:

```text
ApprovalPolicy
    决定当前 Tool Call 是否可以开始执行

ConfinedFileSystem
    即使调用获准,也把文件能力限制在工作区和非保护路径

WorkspaceShell
    只有 cwd 与超时/取消清理,不提供 OS 级隔离
```

### 读完这条主线后

现在应该能够沿源码回答以下问题:

1. 为什么 `approval_class` 必须来自注册后的 `Tool`,而不能来自 Model 的 Tool Call?
2. 一条 Tool Call 在 handler 运行前依次经过哪些检查?
3. `decide()` 里三条 fail-closed 规则(DENY 优先、无 resolver 时 ASK→DENY、resolver 异常
   fail closed)分别防住了什么攻击面?
4. `Injected[FileSystem]` 怎样阻止 Model 选择或替换可信 Environment?
5. `ConfinedFileSystem` 为什么要分"词法检查"和"符号链接解析"两个阶段,只做前者会漏掉什么?
6. 为什么把工作区设为 shell 的 `cwd` 不等于把 shell 限制在工作区?

## 讨论

`WorkspaceShell` 不受路径限制这件事,如果不在文档里写清楚,而是留给用户自己去发现,会有什么后果?
一个"诚实地暴露边界"的设计和一个"假装什么都管住了"的设计,对使用者的实际安全水平有什么不同?如果
只能选一种,你会选哪一种,为什么?

??? success "展开参考答案"

    如果不说明 `WorkspaceShell` 不受路径限制,使用者很可能把"文件工具被限制在工作区"误解成
    "Agent 的所有操作都被限制在工作区",进而在错误的安全假设下授予权限。

    诚实暴露边界不能直接消除风险,但能让使用者据此选择更严格的 approval mode、容器或操作系统级沙
    箱。相比之下,假装已经管住了会制造虚假的安全感,实际风险反而更高。因此应选择诚实暴露边界,同时
    尽可能通过默认拒绝、明确提示和外部隔离进一步缩小风险。
