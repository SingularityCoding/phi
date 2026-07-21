# Chapter 05 · Session 持久化与分支

<div class="phi-chapter-meta" markdown>
<span>概念讲解</span><span>不现场写代码</span><span>对照 phi/sessions/entries.py、phi/sessions/metadata.py、phi/sessions/storage.py、phi/sessions/service.py</span>
</div>

mini-agent 的 `messages` 是一个 Python 列表,活在一次进程运行的内存里。你关掉终端,这次对话就彻
底不存在了——下次运行,又是从零开始的一次新对话。这对一次性的小实验没问题,但只要你把 Agent 当成
一个真正会重复使用的工具,问题立刻就出现了:昨天聊到一半的任务,今天想接着聊,该怎么接?更麻烦的
是,有时候你并不想接着聊——你想回到第 5 轮那个节点,试一条完全不同的路,同时不破坏原来那条已经跑
通的对话。这就是这一章要处理的:让对话活得比一次进程更久,并且允许它长出分支。

## 动手:先感受一次 fork

在 TUI(`uv run phi`,cwd 用这个仓库本身)里:

```text
1. 输入:"介绍一下这个仓库是做什么的"                   等回复完
2. /session                                          记住:Origin: new,Parent Session: -
3. 输入:"如果要给这个项目加一个新功能，你会怎么规划？"    等回复完
4. /fork                                              选中第 1 轮回复后的那个节点
5. /session                                          对比:Origin: fork,Parent Session: <上面的 ID>,
                                                       Fork point: <刚选的 entry>
```

Claude Code 的 session resume 和 rewind 大概也是类似体验——先说说你们平时怎么用这类功能,再往下
看 Phi 具体怎么落地。

## 内存之外,对话要活多久

一次进程结束就丢弃状态,本质上是把"要不要记住"这个决定甩给了操作系统的进程生命周期——这从来都不
是一个刻意的设计,只是"最简单能跑起来的实现"顺手带来的副作用。要让对话真正可用,你至少要能回答两
个问题:这次对话中断后,状态存在哪里、以什么形式存;以及如果我想基于历史某一点尝试别的方向,系统
允许我怎么做,原来的记录会不会被覆盖。

## 主流做法

- **完全不持久化**:每次调用都是无状态的一次性请求,适合简单脚本化场景,但完全谈不上"接着聊"。
- **线性续接**:保存最近一次对话,下次启动时自动续上——只有一条时间线,没有"分支"这个概念。
- **完整的分支/复刻树**:对话历史是一棵树而不是一条线,任意历史节点都可以作为新分支的起点,原节点
  和它之后的历史保持不变。Claude Code 的 session resume/fork 就是这个形状,Phi 也是。

## Phi 怎么做:从 Session ID 恢复一条可继续的分支

这一节要沿源码回答一个具体问题:

> 进程重启后,Phi 只拿到一个 Session ID,怎样恢复用户上次选中的完整对话路径,并且允许从历史节点
> 开出新分支而不覆盖原记录?

在 VS Code 中打开以下四个文件:

```text
phi/src/phi/sessions/entries.py
phi/src/phi/sessions/metadata.py
phi/src/phi/sessions/storage.py
phi/src/phi/sessions/service.py
```

完整数据流:

```text
Session ID
    ↓
metadata envelope + 已提交的 JSONL Entries
    ↓
SessionHandle(当前 leaf 与 revision)
    ↓
从 leaf 沿 parent_id 回溯
    ↓
跨 Fork 接入父 Session 前缀
    ↓
SessionPresentation / ConversationView
    ↓
继续 Run,或从合法边界创建新 Fork
```

### 第一步:Session 持久化了哪些事实

在 `entries.py` 中找到 `EntryBase`。每条对话记录都有自己的身份,并通过 `parent_id` 指向上一条记
录:

```python
class EntryBase(BaseModel):
    id: str = Field(default_factory=_entry_id, min_length=1)
    parent_id: str | None = None
    created_at: datetime = Field(default_factory=_timestamp)
```

`UserMessageEntry`、`AssistantMessageEntry`、`ToolResultEntry` 和 `CompactionEntry` 都沿用这个
结构,所以 Session 内部保存的是一棵 Entry 树,当前对话只是从某个 leaf 回到根的一条路径。

再看 `metadata.py` 里的 `SessionMetadata`:

```python
class SessionMetadata(BaseModel):
    id: str
    leaf_id: str | None = None
    parent_session_id: str | None = None
    fork_point_entry_id: str | None = None
    origin: Literal["new", "fork", "subagent"] = "new"
```

这里同时记录两种关系:`leaf_id` 选择 Session 自己的当前 Entry 路径;`parent_session_id` 和
`fork_point_entry_id` 让一个 Fork Session 引用父 Session 的历史前缀。`lineage_matches_origin()`
把两者的组合限制为三种有效形状:普通 Session 没有父谱系,Fork 同时拥有父 Session 和精确分叉点,
Subagent 只记录委派来源而不继承父对话——所以 Phi 里其实有两层树:Session 内部的 Entry 树,以及
多个 Session 之间的 Fork 谱系。

### 第二步:从磁盘恢复不可变的 SessionHandle

在 `service.py` 中找到 `resume_session()`:

```python
state = await storage.load_state(session_id)
await _session_branch_points(storage, state)
handle = _handle(storage, state.envelope.metadata, state.envelope.revision, diagnostics=state.diagnostics)
await materialize_conversation(storage, handle)
```

`load_state()` 负责磁盘格式与提交一致性(细节见下面的深入阅读);`_session_branch_points()` 和
`materialize_conversation()` 负责结构与对话语义。两层检查都通过后,Host 才得到新的
`SessionHandle`——它是不可变游标,而不是一份可以原地修改的 Session 对象。每次成功追加都会返回带
新 `leaf_id` 和新 `revision` 的 Handle,调用者需要用新值替换旧值(这也是你刚才在 TUI 里
`/fork` 之后,当前 Session 自动切换成新 handle 的原因)。

### 第三步:沿 leaf 物化完整路径

从 `materialize_presentation()` 进入 `_materialize_path()`。这个函数先在当前 Session 内建立
Entry 索引,再从 `leaf_id` 沿 `parent_id` 逆向收集:

```python
by_id = {entry.id: entry for entry in state.entries}
local_reversed: list[Entry] = []
current_id = leaf_id
while current_id in by_id:
    entry = by_id[current_id]
    local_reversed.append(entry)
    current_id = entry.parent_id
```

普通 Session 到本地根就结束。Fork 的本地路径会在 `fork_point_entry_id` 处离开当前 journal,此时
函数加载父 Session,并递归物化父历史:

```python
if metadata.origin != "fork" or current_id != metadata.fork_point_entry_id:
    raise MissingEntryParentError(...)
parent = await storage.load_state(metadata.parent_session_id)
prefix = await _materialize_path(storage, parent, metadata.fork_point_entry_id, seen_sessions=lineage)
```

最终返回"父前缀 + 当前 Session 的本地后缀"。`seen_sessions` 和 `seen_entries` 分别检测 Session
谱系环与 Entry 环;缺失的 parent 也会变成明确的恢复错误,而不会被静默截断。

### 第四步:Fork 如何复用历史而不复制历史

找到 `fork_session()`。它先物化当前选定路径,再检查请求的 `entry_id` 是否真的是这条路径上的合法
分支点:

```python
selected_path = await _materialize_path(...)
if entry_id not in _validate_path(selected_path, handle.session_id):
    raise InvalidSessionLeafError(...)
```

验证通过后,新 Session 只写入引用关系:

```python
envelope = await storage.create(
    parent_session_id=handle.session_id,
    fork_point_entry_id=entry_id,
    origin="fork",
    ...,
)
```

此时新 Fork 的 journal 仍然为空。它通过 `parent_session_id` 和 `fork_point_entry_id` 共享父历
史,之后的新消息才进入自己的 journal;父 Session 继续指向原 leaf,原来的后续记录也保持不变——这
正是你刚才做的那次 `/fork`:

```text
父 Session journal:  U1 → A1 → U2 → A2
                           ↑
                           └── child.fork_point_entry_id

子 Session journal:             U3 → A3

子 Session 的完整路径: U1 → A1 → U3 → A3
```

### 第五步:确认哪些节点可以成为分支点

最后阅读 `_validate_path()`。普通 User 消息和不带工具调用的 Assistant 消息都可以形成完整边界;
带 Tool Call 的 Assistant 消息必须与紧随其后的全部 Tool Result 组成不可分割单元:

```python
expected_ids = [call.id for call in entry.tool_calls]
following = path[index + 1 : index + 1 + len(expected_ids)]
if [item.result.call_id for item in result_entries] != expected_ids:
    raise CorruptSessionError(...)
branch_points.add(result_entries[-1].id)
```

因此不能把 leaf 切到一组 Tool Calls 的中间,也不能从只完成了一部分 Tool Results 的位置 Fork,否
则下一次 Model Request 会看到无法配对的工具消息。`CompactionEntry` 本身也是合法的分支点——它已
经记录了摘要与保留边界,从这里继续或 Fork 都能物化出一份完整、有效的 Conversation View。

??? note "深入阅读(课后):一次追加怎样成为"已提交历史""

    一次消息往返可能产生多条 Entry。`SessionStorage.append_entries()` 先检查调用者持有的
    revision,再构造新的提交信封:

    ```python
    if current.envelope.revision != expected_revision:
        raise StaleSessionHandleError(...)
    updated = SessionMetadataEnvelope(
        revision=current.envelope.revision + 1,
        committed_entry_count=current.envelope.committed_entry_count + len(entries),
        metadata=metadata,
    )
    ```

    `revision` 是乐观并发控制:旧 `SessionHandle` 不能覆盖别人已经完成的追加、改名或 leaf 切换。
    真正的写入顺序是"新 Entries 追加到 `.jsonl` journal 并 `fsync` → metadata 写入临时文件并
    `fsync` → `os.replace` 原子替换 metadata"。真正的提交点是 metadata 中的
    `committed_entry_count`:如果进程在 journal 写完、metadata 更新前崩溃,重启后的
    `_load_state_sync()` 只解析已经提交的前缀,并把尾部报告为诊断:

    ```python
    for index, line in enumerate(lines[:committed]):
        ...
    if len(lines) > committed:
        diagnostics = (f"ignored {len(lines) - committed} uncommitted trailing Entry record(s)",)
    ```

    持久化的核心不变量是:metadata 指向的 leaf 必须属于已提交 Entry;journal 中尚未被 metadata
    确认的内容不能进入 Conversation View。完整实现见 `phi/src/phi/sessions/storage.py` 中的
    `SessionStorage.append_entries()` 和 `_load_state_sync()`。

### 读完这条主线后

现在应该能够沿源码回答以下问题:

1. `leaf_id`、`parent_id` 与 `fork_point_entry_id` 分别连接哪一层关系?
2. 为什么 `SessionHandle` 是不可变游标,而不是一份可以原地修改的对象?
3. 一个空 journal 的 Fork 怎样恢复父 Session 的历史前缀?
4. 为什么 Tool Call 与对应的全部 Tool Results 只能作为一个完整分支边界?
5.(对应深入阅读)为什么 journal 尾部已经写入磁盘的 Entry 仍可能不属于已提交 Session?

## 讨论

如果要求 `sessions/service.py` 一定要拆分成几个更小的模块,你会按什么边界来切——是按"CRUD vs 分支
校验 vs Run 编排"这种职责边界切,还是按别的维度?拆开之后,原来靠"都在同一个文件里"隐含维护的一致
性(比如分叉时父子关系的正确性),要靠什么机制来保证不被破坏?

??? success "展开参考答案"

    一种可行的拆分方式是:

    - Repository 负责 Session 的读取和持久化;
    - lineage 相关服务负责父子关系与分支校验;
    - Run service 负责把一次新输入交给 Harness;
    - Context projection 负责从历史中构建和检查 Context。

    拆分之后,关键不只是"文件变小了",而是所有会改变 Session 状态的操作仍然必须经过少数几个明确入
    口。父子关系、entry 顺序等不变量应由这些入口集中校验,并通过事务、类型约束和跨模块测试保证,而不
    能依赖调用者自觉维护。
