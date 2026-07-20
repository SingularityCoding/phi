# Chapter 08 · Hooks & Skills

<div class="phi-chapter-meta" markdown>
<span>概念讲解</span><span>不现场写代码</span><span>对照 phi/harness/、phi/skills/、phi/bootstrap.py</span>
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

## Phi 怎么做：沿行为扩展与知识扩展两条路径

这一章的源码阅读围绕一个问题展开：外部能力需要改变 Agent 行为时，怎样进入 Harness；只需要补充领域
知识时，又怎样避免让全部内容长期占用 Context？

先在 VS Code 中打开以下文件：

```text
phi/src/phi/harness/hooks.py
phi/src/phi/harness/run.py
phi/src/phi/skills/discovery.py
phi/src/phi/skills/invocation.py
phi/src/phi/bootstrap.py
```

两条扩展路径可以先概括为：

```text
行为扩展：Hooks → Harness 在固定边界读取返回值 → 改变接下来的控制流
知识扩展：Skill 文件 → 发现并展示精简目录 → skill_tool 按名称返回正文
```

### 第一步：从三个 Hook 的类型开始

在 `harness/hooks.py` 中找到 `Hooks`。它只提供三个字段：

```python
@dataclass(frozen=True)
class Hooks:
    before_tool_call: ApprovalPolicy | None = None
    before_run_complete: CompletionHook | None = None
    inject_messages: MessageInjectionHook | None = None
```

先根据类型判断每个 Hook 能影响什么：

| Hook | 调用时机 | 返回值影响 |
| --- | --- | --- |
| `inject_messages` | 每个 Step 开始前 | 向当前 Run 追加新的 User 消息 |
| `before_tool_call` | Tool Call 执行前 | 允许或拒绝这次调用 |
| `before_run_complete` | Model 给出最终文本后 | 接受结果，或携带反馈继续一个 Step |

`CompletionDecision.__post_init__()` 还把状态组合限制得很明确：`RETRY` 必须带非空反馈，
`ACCEPT` 不能夹带反馈。Hook 的返回值先被验证，再参与控制流。

### 第二步：在 Run 循环中找到三个消费位置

切换到 `harness/run.py`，依次搜索 `inject_messages`、`before_tool_call` 和
`before_run_complete`。这三个位置分布在一次 Step 的不同阶段：

```text
Step 开始
  → 排空 inject_messages
  → 调用 Model
  → before_tool_call 决定 Tool Call 是否获准
  → 得到候选最终文本
  → before_run_complete 决定接受或重试
```

消息注入发生在 Model Request 构造之前：

```python
injected_messages = await active_hooks.inject_messages()
working_messages.extend(
    {"role": "user", "content": message}
    for message in injected_messages
)
```

因此 Steer 不会取消正在进行的 Model 请求，只会在下一个 Step 边界生效。

`before_tool_call` 则被传给同一个 `ToolDispatcher`：

```python
result = await dispatcher.dispatch(
    deepcopy(call),
    approval_policy=active_hooks.before_tool_call,
    approval_observer=observe_approval,
)
```

Hook 可以替换这次分发所用的 approval policy，但工具查找、参数校验、审批与执行仍然只有
Dispatcher 这一条边界。

### 第三步：确认 Hook 不能绕开 Run 的总预算

继续查看完成 Hook 的 `RETRY` 分支：候选 Assistant 响应和纠正反馈会被追加到
`working_messages`，然后进入下一个 Step。

```python
working_messages.append(serialize_assistant_response(response))
working_messages.append({"role": "user", "content": decision.feedback})
```

这里没有创建第二套重试循环。工具往返与完成 Hook 重试都消耗同一个 `max_steps`。最后一个 Step 上的
`RETRY` 会返回 `RunStatus.MAX_STEPS`，因此 Hook 有权请求继续，但没有权把有界 Run 变成无限循环。

这条不变量可以在 `tests/harness/test_hooks.py` 的
`test_completion_retry_on_the_final_step_returns_max_steps` 中得到验证。

### 第四步：从 Skill 文件到可信的 Skill 对象

现在沿知识扩展路径阅读。在 `skills/discovery.py` 中找到 `discover_skills()`：

```python
return SkillDiscovery(
    skills={**global_skills, **project_skills},
    diagnostics=(...),
)
```

全局目录和项目目录分别扫描，最后由项目级同名 Skill 覆盖全局定义。再进入 `_load_skill()`，观察一个
Skill 可以是目录中的 `SKILL.md`，也可以是独立 Markdown 文件；两种形式都需要经过 YAML
frontmatter、文件系统名称、小写连字符命名规则，以及类型严格为 `bool` 的
`disable-model-invocation` 校验。

单个文件解析失败只生成 `SkillDiagnostic`，不会阻断其他有效 Skill。这使扩展发现具备两条同时成立的
性质：无效内容不会进入运行时，局部错误也不会拖垮整个能力目录。

### 第五步：Context 中先放目录，正文通过 Tool 按需加载

切换到 `skills/invocation.py`，对照 `render_model_skill_menu()` 和 `build_skill_tool()`。

稳定 system prompt 中加入的是精简目录：

```text
- `skill-name`: description
```

真正的 Skill 正文保存在 `skill_tool` 的闭包里。Model 只有明确调用这个只读 Tool 并给出精确名称，
才会拿到 `skill.content`：

```python
skill = available.get(name)
if skill is None:
    return ToolFailure(
        "skill_unavailable: no Model-invocable Skill has that exact name"
    )
return skill.content
```

`disable-model-invocation: true` 的 Skill 在建立闭包前就被排除。Model 即使猜中名称，得到的也只是统一的
不可用错误，正文和来源路径都不会泄露；可信用户仍可通过 `invoke_user_skill()` 精确调用它。

### 第六步：在运行时装配处合并两部分成本

最后在 `bootstrap.py` 中查看 `assemble_instruction_assembly()` 和
`build_runtime_resources()`。前者把 Skill 目录组装进稳定指令，后者把 `skill_tool` 注册到公共
`ToolRegistry`：

```python
skill_tool = build_skill_tool(discovery.skills)
if skill_tool is not None:
    registry.register(skill_tool)
```

因此 Skill 的成本被拆成两部分：每次请求携带简短的名称与描述；只有真正相关时，正文才通过一次普通
Tool Call 进入对话。它复用了既有的 Tool Result、Event、Trace 和 Session 路径，没有为 Skill 建立
隐藏的执行机制。

### 读完这条主线后

现在应该能够沿源码回答以下问题：

1. 三个 Hook 分别位于一次 Step 的哪个边界？
2. 为什么 `before_run_complete` 无法突破 `max_steps`？
3. 一个损坏的 Skill 文件为什么不会阻止其他 Skill 被加载？
4. `disable-model-invocation` 怎样同时限制 Model、保留可信用户的调用能力？
5. Phi 怎样让 Skill 可被发现，同时避免正文始终占用 Context？

## 讨论

`before_run_complete` 允许 Hook 强制一次 Run 重跑,这意味着一个写得不好的 Hook 理论上可以让 Run 无
限重试下去。核心循环应该在哪里、用什么机制,防止一个 Hook 的错误决定变成一个死循环?这个责任应该
放在 Hook 的实现者身上,还是应该由 Harness 兜底?

??? success "展开参考答案"

    Hook 实现者应负责正确实现和测试自己的逻辑，但 Harness 不能把“永不死循环”寄托在所有 Hook 都
    不会犯错上。

    Harness 应维护一个不可绕过的上限，例如限制一次 Run 能被 Hook 强制重跑的次数，或为 Hook 重试
    设置独立预算。超过上限后，Run 应以明确的失败原因停止并产生可观察的 Event。这样 Hook 可以提出
    “再跑一次”，但最终是否继续仍由 Harness 的有界循环决定。
