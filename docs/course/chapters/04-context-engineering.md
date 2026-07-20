# Chapter 04 · Context Engineering

<div class="phi-chapter-meta" markdown>
<span>概念讲解</span><span>不现场写代码</span><span>对照 phi/harness/context.py、phi/harness/compaction.py、phi/sessions/service.py</span>
</div>

我们在 mini-agent 里跑通的循环,有一个没说出口的假设:每一步都把完整的历史消息原封不动地发给模型。
这个假设短任务里没问题,但只要循环跑得够久,历史会一直变长——而模型能接受的输入是有上限的。这一章
不写代码,我们花时间想清楚:历史长起来之后,应该怎么办。

## 先分清楚几个容易混的词

"聊天记录"这个说法太笼统,拆开看至少有四层:

- **完整历史**——这次会话从头到尾实际发生的一切,一个字都不会丢。
- **Context**——某一次模型调用时,从完整历史里"投影"出来的那一份,受限于模型的输入上限。
- **Trace**——给开发者/给你自己看的完整记录,不等于喂给模型的那份。
- 用户在界面上看到的,也不一定和以上任何一份完全一样。

这四层可以互相不一样,而且大多数时候就应该不一样——这正是这一章要讲的东西。

## 主流做法

- **硬截断**:超过长度就从最老的消息开始丢。简单,但丢掉的信息彻底没了,模型可能会重复问已经问过
  的问题。
- **摘要式压缩(compaction)**:把要丢的那一段历史,让模型自己总结成一段话,替换掉原文,原文仍然留
  在完整历史/Trace 里,只是不再进入 Context。
- **滑动窗口 + 检索**:只保留最近若干轮原文,更早的内容存起来,需要的时候按相关性检索回来——更接
  近搜索引擎的思路,实现复杂度也更高。

这几种做法不是互斥的,现实中的 Agent 经常组合使用。

## Phi 怎么做：追踪一次 Model Request 如何构建与压缩

Session 里保存了很多 Entry，Phi 最终怎样决定这一次 Model 调用到底能看到什么？

在 VS Code 中打开以下三个文件：

```text
phi/src/phi/sessions/service.py
phi/src/phi/harness/context.py
phi/src/phi/harness/compaction.py
```

完整的主线可以先压缩成一张图：

```text
Session 中选定的 Entry 路径
        ↓
SessionPresentation
        ↓
ConversationView
        ↓
Context
        ↓
ModelRequest
        ↓
估算是否超过安全上限
        ├── 没有超过 → 交给 Harness
        └── 超过     → 选择旧历史 → 生成摘要 → 写入 CompactionEntry
```

接下来沿着这条路径逐步展开。网页中的代码只保留关键骨架，完整实现以右侧源代码为准。

### 第一步：找到整条投影链

在 `sessions/service.py` 中搜索 `inspect_context()`，先看下面四行：

```python
presentation = await materialize_presentation(storage, handle)
view = _conversation_view_from_path(handle, presentation.entries)
context = _context_for_view(view, tools, instructions.stable_instructions)
request = context.to_request(model=selected_model)
```

这四行对应四种不同的数据：

1. `SessionPresentation` 是当前 Session 选定路径上的完整持久化记录，供 Host 展示和导航。
2. `ConversationView` 是从这条路径中投影出的有效对话；如果发生过 Compaction，更早的原始 Entry 不再
   进入这个 View。
3. `Context` 把稳定指令、工具 schema、对话消息和历史摘要分开保存。
4. `ModelRequest` 才是即将交给 Model adapter 的 wire-format 请求。

这里最重要的关系是：Phi 不会直接把 Session 交给 Model。Session 必须先经过 Conversation View 和
Context 两层投影。

继续看 `ContextInspection.projection` 中记录的四个数量：

```python
ProjectionCounts(
    session_path_entries=len(presentation.entries),
    conversation_view_entries=len(view.entries),
    context_messages=len(context.messages),
    request_messages=len(request.messages),
)
```

这几个数字不一定相等。发生 Compaction 后，Session 路径仍然保留原始 Entry，但 Conversation View 会
变短；最终请求还会额外加入 system prompt 和摘要消息。

### 第二步：看清 Model 最终收到了什么

切换到 `harness/context.py`，找到 `Context.to_request()`。它按固定顺序构造请求：

```python
messages = [
    {"role": "system", "content": self.system_prompt},
]
if self.dropped_summary is not None:
    messages.append(
        {
            "role": "system",
            "content": "Dropped conversation history summary:\n" + self.dropped_summary,
        }
    )
messages.extend(deepcopy(list(self.messages)))
```

因此，一次请求中的消息顺序是：

```text
稳定的 system instructions
→ 被压缩历史的摘要（如果存在）
→ 当前 Conversation View 中保留的原始消息
```

注意两个细节：

- 摘要作为一条单独的 `system` message 注入，不会伪装成用户或 Assistant 曾经说过的话。
- `Context` 和 `ModelRequest` 不共享可变的字典或列表。构造请求时会执行深拷贝，Model adapter 不能反
  向修改已经构建好的 Context。

回到 `sessions/service.py`，查看 `_context_for_view()`。这里把类型化的 Session Entry 转换成 Model 能
理解的消息：

```python
if isinstance(entry, UserMessageEntry):
    ...
elif isinstance(entry, AssistantMessageEntry):
    ...
elif isinstance(entry, ToolResultEntry):
    ...
```

`CompactionEntry` 没有被转换成普通对话消息。它携带的摘要已经通过 `view.dropped_summary` 单独进入
Context。

### 第三步：理解 Phi 为什么在请求失败前开始压缩

在 `sessions/service.py` 的 `_continue_send()` 中找到预算判断：

```python
effective_limit = effective_input_limit(model_info, policy)
safe_limit = safe_prompt_limit(effective_limit, policy)
estimate = estimate_prompt_tokens(
    request,
    model_id=selected_model or "<unresolved>",
    anchor=handle.prompt_budget_anchor,
)
if should_compact(estimate.tokens, safe_limit, policy):
    handle = await _compact(...)
```

这里有三个不同的数字：

```text
effective input limit
    = Model 声明的输入上限与本地配置上限中更小的一个

safe prompt limit
    = effective input limit - reserve_tokens

estimated prompt tokens
    = 对这次完整请求的 token 估算
```

Phi 比较的是 `estimated prompt tokens` 和 `safe prompt limit`。`reserve_tokens` 为本轮输出预留空间；
如果 prompt 已经占满整个 Context Window，Model 即使接受了输入，也没有足够空间生成答案。

接着打开 `harness/compaction.py`，查看 `estimate_request_tokens()`。它使用确定性的本地公式估算完整的
messages 和 tools，不需要额外调用 Model。

本地公式不可能对所有 tokenizer 都完全准确，因此 Phi 还会保存上一次请求中 Provider 报告的
`prompt_tokens`，形成 `PromptBudgetAnchor`：

```python
anchored = anchor.prompt_tokens + max(0, local - anchor.local_estimate)
return PromptEstimate(max(local, anchored), local, True)
```

锚点只在 Model、tools 和消息前缀仍然匹配时生效，并且只能把估算向上修正，不能让估算变得比本地结果
更乐观。

### 第四步：决定哪些历史可以被摘要

在 `sessions/service.py` 中查看 `_atomic_units()`，再进入 `compaction.py` 中的
`select_compaction_units()`。

`AtomicConversationUnit` 表示 Compaction 不允许拆开的最小单元：

```python
@dataclass(frozen=True)
class AtomicConversationUnit:
    first_entry_id: str
    messages: tuple[dict[str, Any], ...]
    pending_user: bool = False
```

普通 User 或 Assistant 消息可以各自形成单元，但一次 Assistant Tool Call 和它对应的全部 Tool Results
必须放在同一个单元中。否则 Context 可能保留 Tool Result，却丢失产生它的 Tool Call。

接着看强制保留的尾部：

```python
mandatory_count = 2 if units and units[-1].pending_user and len(units) >= 2 else 1
```

如果最后一个单元是刚刚提交、尚未回答的 User 消息，Phi 会同时保留它和前一个完整单元。只有一条孤立
的新问题，往往不足以提供回答所需的近期上下文。

剩余空间从最近的历史开始向更早的位置贪心扩展：

```python
for older in reversed(units[:-mandatory_count]):
    ...
    if current_size - base_size >= settings.keep_recent_tokens:
        break
    ...
    retained = [older, *retained]
```

最后得到两个集合：

```text
dropped  → 交给摘要 Model
retained → 继续以原文进入 Context
```

这里会从近到远保留近期原文，达到 `keep_recent_tokens` 目标后停止，同时确保最终请求不越过安全上限。

### 第五步：把摘要看成一次独立的 Model 调用

回到 `sessions/service.py`，查看 `_compact()`。它依次完成五件事：

```text
1. 物化当前 Conversation View
2. 选择 dropped 与 retained 单元
3. 为 dropped 历史构造摘要请求
4. 调用 Model 生成摘要
5. 将结果写入新的 CompactionEntry
```

摘要请求由 `_summary_request()` 构造，并且明确不提供任何工具：

```python
return ModelRequest(
    messages=[...],
    tools=[],
    model=model_id,
    max_tokens=max_tokens,
)
```

这次调用只是一次文本转换，不是新的 Agent Run。Phi 不向摘要请求提供 Tool，也不会为它启动工具循环；
如果响应仍然包含 Tool Call，就把它判定为无效摘要。

返回值还必须经过验证：

```python
if response.tool_calls or response.content is None or not response.content.strip():
    raise InvalidCompactionSummaryError(...)
```

生成摘要后，Phi 会用真实摘要和保留消息重新构造一次请求，再检查它是否确实能放进安全上限。前面的切分
只使用了摘要占位和最大输出预算，不能代替这次最终验证。

### 第六步：确认 Compaction 如何改变后续 Context

摘要验证成功后，Phi 追加一个新的 `CompactionEntry`：

```python
compaction = CompactionEntry(
    parent_id=handle.leaf_id,
    summary=summary,
    tokens_before=estimate.tokens,
    tokens_before_source=...,
    first_kept_entry_id=selection.first_kept_entry_id,
)
return await _append(storage, handle, (compaction,))
```

这个 Entry 记录旧历史的摘要、压缩前 token 数的来源，以及从哪个 Entry 开始继续保留原文。它没有删除或
覆盖更早的 Entry。

下一次调用 `materialize_conversation()` 时，最新的 `CompactionEntry` 会决定新的 Conversation View：

```text
dropped_summary = CompactionEntry.summary
visible entries = first_kept_entry_id 之后的原始 Entry
```

因此：

- Session 仍然保存完整的持久化路径；
- Host 仍然可以展示较早的记录；
- Model 看到的是摘要加近期原文；
- 再次 Compaction 时，旧摘要也会进入新的摘要请求，避免更早历史静默消失。

### 第七步：最后看失败时怎样停止

读完正常路径后，回到 `_continue_send()` 看失败分支。

| 情况 | Phi 的处理 |
| --- | --- |
| Model 元数据与本地配置都没有输入上限 | 跳过主动 Compaction，并记录诊断信息 |
| 必须保留的近期消息已经放不下 | 抛出 `ContextCapacityError` |
| 没有更早的完整单元可以摘要 | 抛出 `NothingToCompactError` |
| 摘要为空或包含 Tool Call | 抛出 `InvalidCompactionSummaryError` |
| Compaction 后重建的请求仍然超限 | 停止，不继续无限摘要 |
| Provider 意外返回 Context overflow | 仅在尚未压缩且尚未产生任何 Tool Result 时尝试一次恢复 |

最后一个限制由下面的条件保证：

```python
not any(step.tool_results for step in result.steps)
```

这个条件会保守地禁止重放任何已经处理完成的 Tool Call，包括只读调用和预期失败。因为 Phi 不能仅凭
`ToolResult` 证明调用一定没有副作用，所以一旦出现过 Tool Result，就停止这次自动恢复。

### 读完这条主线后

现在应该能够沿源码回答以下问题：

1. Session、Conversation View、Context 和 ModelRequest 分别在哪一步产生？
2. 为什么摘要不直接替换或删除旧的 Session Entry？
3. 为什么 Tool Call 和 Tool Result 必须作为一个整体参与 Compaction？
4. 为什么摘要调用不允许使用工具？
5. 为什么 Provider 报出 Context overflow 后不能无条件自动重试？

## 讨论

摘要式压缩有一个天然的风险:被总结掉的那段历史里,如果藏着一个后面还会用到的关键细节(比如用户在
第 3 轮随口提到的一个文件路径),摘要有没有可能把它漏掉?如果漏了,模型在第 20 轮会表现出什么样的
症状?你觉得这个风险,应该靠"让摘要写得更仔细"来解决,还是靠"某些内容干脆标记为不可压缩"来解决?

??? success "展开参考答案"

    摘要写得更仔细只能降低遗漏概率，不能保证关键细节一定被保留。一旦文件路径、约束条件或已经做出的
    决定被漏掉，模型可能会重复搜索、违反旧约束，或者给出与前文矛盾的结果。

    更稳妥的设计是组合多种机制：普通对话可以摘要压缩；文件路径、用户约束、任务状态等结构化信息单独
    保存；必要时再从完整 Session 中检索原文。被标记为不可压缩的内容也需要有数量和生命周期限制，否
    则它本身最终会撑满 Context。
