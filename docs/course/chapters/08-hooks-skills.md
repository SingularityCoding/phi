# Chapter 08 · Hooks & Skills

<div class="phi-chapter-meta" markdown>
<span>概念讲解</span><span>不现场写代码</span><span>对照 phi/harness/hooks.py、phi/harness/run.py、phi/skills/discovery.py、phi/skills/invocation.py、phi/bootstrap.py</span>
</div>

到目前为止,想改变 Agent 在某个环节的行为,唯一的办法是去改循环本身的代码。但并不是每一次定制化需
求都值得去动核心循环——你可能只是想在某个工具被调用前额外检查一下,或者想让 Agent 具备某个专门领
域的知识,而这个知识大多数时候根本用不上,没必要一直挤占 Context。这一章讲两件相关但不同的事:怎
么在不改核心代码的前提下介入循环的行为,以及怎么按需给模型补充领域知识。

## 定制化不应该等于改源码

如果每个人的每一点定制需求都要求 fork 代码库、改核心循环,这个系统很快就会变得没法维护——每个使
用者的分支都会和主线越走越远。要解决这个问题,核心循环需要主动暴露出几个明确的"接口点",让外部行
为可以挂进来,而不用触碰循环内部的实现。

## 主流做法

- **生命周期脚本挂钩**:在预定义的几个节点(工具调用前、会话开始时等)触发外部脚本——Claude Code
  自己的 Hooks 机制就是这个形状。
- **插件/扩展 API**:提供更结构化的扩展接口,而不只是脚本触发点。
- 面向领域知识补充,常见形态包括:斜杠命令、静态的、始终加载的自定义指令,以及按需加载、只有相关
  时才会被拉进 Context 的"技能"文件——Claude Code 自己的 Skills 功能就是最后这种形态。

## Phi 怎么做:沿行为扩展与知识扩展两条路径

这一章的源码阅读围绕一个问题展开:外部能力需要改变 Agent 行为时,怎样进入 Harness;只需要补充领域
知识时,又怎样避免让全部内容长期占用 Context?

在 VS Code 中打开以下文件:

```text
phi/src/phi/harness/hooks.py
phi/src/phi/harness/run.py
phi/src/phi/skills/discovery.py
phi/src/phi/skills/invocation.py
phi/src/phi/bootstrap.py
```

两条扩展路径:

```text
行为扩展:Hooks → Harness 在固定边界读取返回值 → 改变接下来的控制流
知识扩展:Skill 文件 → 发现并展示精简目录 → skill_tool 按名称返回正文
```

### 第一步:三个 Hook 分别管什么

在 `harness/hooks.py` 中找到 `Hooks`,它只有三个字段:

```python
@dataclass(frozen=True)
class Hooks:
    before_tool_call: ApprovalPolicy | None = None
    before_run_complete: CompletionHook | None = None
    inject_messages: MessageInjectionHook | None = None
```

| Hook | 调用时机 | 返回值影响 |
| --- | --- | --- |
| `inject_messages` | 每个 Step 开始前 | 向当前 Run 追加新的 User 消息 |
| `before_tool_call` | Tool Call 执行前 | 允许或拒绝这次调用 |
| `before_run_complete` | Model 给出最终文本后 | 接受结果,或携带反馈继续一个 Step |

`CompletionDecision.__post_init__()` 把状态组合限制得很明确:

```python
if self.decision is RunDecision.RETRY and (
    not isinstance(self.feedback, str) or not self.feedback.strip()
):
    raise ValueError("retry completion decisions require non-empty feedback")
if self.decision is RunDecision.ACCEPT and self.feedback is not None:
    raise ValueError("accepted completion decisions cannot include feedback")
```

`RETRY` 必须带非空反馈,`ACCEPT` 不能夹带反馈——Hook 的返回值先被验证,再参与控制流。

### 第二步:三个 Hook 在 Run 循环里的具体位置

切到 `harness/run.py`。每个 Step 开始时先排空 `inject_messages`,再构造这一次的 Model Request:

```python
if active_hooks.inject_messages is not None:
    injected_messages = await active_hooks.inject_messages()
    ...
    working_messages.extend(
        {"role": "user", "content": message} for message in injected_messages
    )
```

因此 Steer 不会取消正在进行的 Model 请求,只在下一个 Step 边界生效。`before_tool_call` 被直接传
给同一个 `ToolDispatcher`(和第 06 章讲过的 `dispatch()` 是同一个函数):

```python
result = await dispatcher.dispatch(
    deepcopy(call),
    approval_policy=active_hooks.before_tool_call,
    approval_observer=observe_approval,
)
```

Hook 可以替换这次分发所用的 approval policy,但工具查找、参数校验、审批与执行仍然只有 Dispatcher
这一条边界。Model 给出候选最终文本后,`before_run_complete` 才被调用:

```python
decision = await active_hooks.before_run_complete(_snapshot_result(provisional_result))
if not isinstance(decision, CompletionDecision):
    raise TypeError("before_run_complete must return CompletionDecision")
```

### 第三步:Hook 不能绕开 Run 的总预算

`RETRY` 分支把候选 Assistant 响应和纠正反馈追加到 `working_messages`,然后进入下一个 Step:

```python
working_messages.append(serialize_assistant_response(response))
working_messages.append({"role": "user", "content": decision.feedback})
if step_index + 1 == max_steps:
    return await _finish(emitter, RunResult(RunStatus.MAX_STEPS, tuple(steps)))
```

这里没有创建第二套重试循环。工具往返与完成 Hook 重试都消耗同一个 `max_steps`。最后一个 Step 上
的 `RETRY` 会返回 `RunStatus.MAX_STEPS`,因此 Hook 有权请求继续,但没有权把有界 Run 变成无限循
环。这条不变量可以在 `tests/harness/test_hooks.py` 的
`test_completion_retry_on_the_final_step_returns_max_steps` 中得到验证。

### 第四步:Skill 目录常驻 Context,正文按需通过 Tool 加载

现在切换到知识扩展路径。`skills/discovery.py` 的 `discover_skills()` 分别扫描全局和项目目录,项
目同名 Skill 覆盖全局定义;单个文件解析失败只生成 `SkillDiagnostic`,不阻断其他有效 Skill:

```python
try:
    skill = _load_skill(source_path)
except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, ValueError) as error:
    diagnostics.append(SkillDiagnostic(source_path, str(error)))
    continue
```

真正决定 Context 成本的是 `skills/invocation.py`。稳定 system prompt 中只放精简目录:

```python
def render_model_skill_menu(skills: Mapping[str, Skill]) -> str:
    available = model_invocable_skills(skills)
    entries = "\n".join(
        f"- `{skill.name}`: {' '.join(skill.description.split())}" for skill in available
    )
    return "Load a Skill by calling `skill_tool` with its exact name.\n" + entries
```

Skill 正文保存在 `skill_tool` 的闭包里,Model 只有明确调用这个只读 Tool 并给出精确名称,才能拿到
`skill.content`:

```python
async def load_skill(name: str) -> str | ToolFailure:
    skill = available.get(name)
    if skill is None:
        return ToolFailure("skill_unavailable: no Model-invocable Skill has that exact name")
    return skill.content
```

`disable-model-invocation: true` 的 Skill 在建立 `available` 闭包前就被排除,Model 即使猜中名
称,得到的也只是统一的不可用错误,正文和来源路径都不会泄露;可信用户仍可通过
`invoke_user_skill()` 精确调用它。最后在 `bootstrap.py` 里,`skill_tool` 和其他 Tool 一样注册到
公共 `ToolRegistry`,没有专门的执行路径:

```python
skill_tool = build_skill_tool(discovery.skills)
if skill_tool is not None:
    registry.register(skill_tool)
```

Skill 的成本因此被拆成两部分:每次请求携带简短的名称与描述;只有真正相关时,正文才通过一次普通
Tool Call 进入对话,复用既有的 Tool Result、Event、Trace 和 Session 路径。

### 读完这条主线后

现在应该能够沿源码回答以下问题:

1. 三个 Hook 分别位于一次 Step 的哪个边界?
2. `before_tool_call` 替换的是审批策略,还是绕开了 Dispatcher 本身?
3. 为什么 `before_run_complete` 无法突破 `max_steps`?
4. 一个损坏的 Skill 文件为什么不会阻止其他 Skill 被加载?
5. `disable-model-invocation` 怎样同时限制 Model、保留可信用户的调用能力?

## 讨论

`before_run_complete` 允许 Hook 强制一次 Run 重跑,这意味着一个写得不好的 Hook 理论上可以让 Run 无
限重试下去。核心循环应该在哪里、用什么机制,防止一个 Hook 的错误决定变成一个死循环?这个责任应该
放在 Hook 的实现者身上,还是应该由 Harness 兜底?

??? success "展开参考答案"

    Hook 实现者应负责正确实现和测试自己的逻辑,但 Harness 不能把"永不死循环"寄托在所有 Hook 都
    不会犯错上。

    Harness 应维护一个不可绕过的上限,例如限制一次 Run 能被 Hook 强制重跑的次数,或为 Hook 重试
    设置独立预算。超过上限后,Run 应以明确的失败原因停止并产生可观察的 Event。这样 Hook 可以提出
    "再跑一次",但最终是否继续仍由 Harness 的有界循环决定。
