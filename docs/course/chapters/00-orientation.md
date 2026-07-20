# Chapter 00 · 开场:Phi 全景 + 并发回顾

<div class="phi-chapter-meta" markdown>
<span>演示 + 回顾</span><span>不写代码</span><span>为 01-12 章打地基</span>
</div>

正式开始之前，花一点时间把两件事摆清楚:接下来六个小时最终要抵达的地方长什么样，以及待会写
`async def request()` 的时候，那两个词到底在干什么。

## Part A · Phi 全景演示

先不解释内部实现，我们直接完整跑一次 Phi。

看演示的时候，先留意三个问题：

1. 哪些动作是 Model 提议的？
2. 哪些动作必须由 Harness 才能真正执行？
3. 一次 Run 结束以后，哪些信息会被保留下来？

后面的章节会逐一回答这些问题。

### 1 · 启动 Phi

在一个准备好的演示目录中启动 TUI：

```bash
uv run phi
```

现在看到的是 Phi 的交互式 Host。我们可以在这里提交任务，观察 Model 的输出、Tool Call、Tool Result 和最终回复。

先确认当前使用默认的审批模式：

```text
/permissions default
```

### 2 · 交给它一个真实任务

演示目录中已经有一份 `brief.md`。现在提交任务：

```text
读取 brief.md，把其中的信息整理成一份带标题和三个要点的摘要，
写入 result.md，然后运行一个只读命令确认文件已经生成。
最后告诉我你读取了什么、修改了什么、如何验证。
```

先观察它如何向前推进：

```text
Model 请求读取文件
    ↓
Harness 执行 Tool
    ↓
Tool Result 返回给 Model
    ↓
Model 根据结果决定下一步
```

一次任务不一定只调用一次 Model。只要 Model 还在请求 Tool，Harness 就会把结果放回 Context，再开始下一步。这段反复发生的控制流程就是 Agent Loop。

第 01–03 章会从零写出这里看到的 Model 边界、Tool 边界和 Agent Loop。

### 3 · 谁拥有执行权

当 Phi 准备写入 `result.md` 时，Run 会停在审批界面。

先看清楚审批界面中的信息：

- 它准备调用哪个 Tool；
- 它准备传入什么参数；
- 这个操作属于哪一类权限；
- 我们可以只允许这一次、在当前 Session 中持续允许，或者拒绝。

这里要区分两件事：

> Model 提议写文件，但 Model 自己不能写文件。
>
> Harness 验证 Tool Call、请求授权，并决定是否执行。

选择“仅本次允许”，让 Run 继续完成。第 06 章会再回来拆解这条安全边界。

### 4 · 最终回复不等于真实结果

Run 完成以后，Phi 会说明它读取了什么、修改了什么，以及如何验证。

但 Agent 说“文件已经生成”，并不能证明文件真的存在。打开 `result.md`，再检查一次 Environment 中的实际状态。

这里的 Environment 才是 ground truth。Agent 的最终回复只是它对结果的描述。

### 5 · Model 实际看到了什么

接着输入：

```text
/context
```

这里可以看到本次请求的 Overview、Contents 和 Raw request。

注意三种东西并不相同：

- TUI 中展示的是 Conversation；
- Model 每一步收到的是由 Harness 构建的有限 Context；
- Session 保存的是可恢复、可继续使用的持久化历史。

它们现在看起来很接近，但随着 Conversation 变长，三者之间的区别会越来越重要。第 04 章会专门讨论 Context 是怎样构建出来的。

退出 Context 页面，再输入：

```text
/session
```

记下当前的 Session ID。虽然刚才的 Run 已经结束，这段 Conversation 仍然属于一个可以继续使用的 Session。

### 6 · 恢复和分叉

退出 TUI：

```text
/quit
```

然后重新打开刚才的 Session：

```bash
uv run phi session resume <session-id>
```

之前的用户消息、Model 回复、Tool Call 和 Tool Result 都重新出现了。这不是重新执行刚才的任务，而是从持久化数据中恢复 Conversation。

现在输入：

```text
/fork
```

选择第一次用户消息作为分叉点，然后给出一个不同的后续要求：

```text
改用表格整理 brief.md，并且不要写文件，只在回复中展示结果。
```

再输入一次：

```text
/session
```

此时我们已经位于一个新的 Session。它保存了自己的 Session ID，同时记录 Parent Session 和 Fork point。

恢复是在原 Session 上继续；Fork 则从一段已有历史中建立新的 Session。第 05 章会具体查看它们怎样存储。

### 刚才完整发生了什么

回头看刚才的任务：

```text
Host 接收任务
    ↓
Harness 构建 Context
    ↓
Model 返回回复或 Tool Call
    ↓
Harness 验证、授权并执行 Tool
    ↓
Tool Result 回到下一步 Context
    ↓
Harness 决定继续或停止
    ↓
Session 保存 Conversation
```

这就是我们接下来要拆开的完整系统：

> **Agent = Model + Harness**

现在先暂时放下 Context、Session 和 Safety，从最小的 Model 边界开始。

## Part B · Python 并发回顾

如果你做完了课前的 [Async Lab](../prework/async-lab/index.md)，`coroutine`、`Task`、`await` 挂起
恢复、`TaskGroup`、cancellation 这些你已经亲手跑过、亲手破坏过了——这里不重复那些练习，只是花几分
钟把"并发"这件事的整张地图重新摊开，把 async/await 摆回它在这张地图里的位置，再补一块 Async Lab
明确没讲、但你今天就会用到的东西。

### 并发这件事，Python 里有几种活法

同一件事——"让程序同时应付好几件事情"——Python 里至少有三条路，选哪条取决于你到底在等什么:

- **多进程（multiprocessing）**：真正的并行，每个进程有自己独立的内存空间和解释器，能吃满多个
  CPU 核心。代价是重——启动开销大，进程间传数据要序列化。适合 CPU 密集型计算。
- **多线程（threading）**：线程之间共享内存，切换比进程轻。但 Python 有个绕不开的东西——
  **GIL**（Global Interpreter Lock）：同一时刻，只有一个线程在执行 Python 字节码。这意味着纯计算
  型的任务，多开几个线程并不会更快。但线程仍然有意义：当一个线程在等 I/O（读文件、等网络）的时
  候，它会**释放** GIL，让别的线程有机会跑——所以多线程对 I/O 密集型任务依然有效，只是这个"有效"
  跟"多核并行"是两回事。
- **协程（coroutines）/ `async`/`await`**：单个线程，但这一个线程可以在多件事之间"协作式"地来回
  切换——不是抢占式的（没人会在你一行代码中间把控制权抢走），而是代码自己在明确的挂起点（`await`）
  主动让出控制权。这正是 Async Lab 让你亲手验证过的事情:调用一个 `async def` 函数不会立刻执行它，
  只会拿到一个 coroutine object；只有真正 `await` 它，或者把它交给 event loop 调度成一个 Task，它
  才会真的往前推进。

### event loop 在这幅图里的角色

一句话:event loop 是那个"决定接下来该轮到谁跑"的调度者。它手上有一份"准备好可以继续跑"的任务清
单；当前正在跑的协程遇到一个还没完成的 `await`，就把控制权交回给它；它转头去推进别的已经就绪的任
务；等到刚才那个 `await` 的条件满足了，原来的协程会在挂起的地方原样恢复——不是重新跑一遍，是接着
往下走。

这跟多线程完全是两回事：这里始终只有一个线程在跑 Python 代码，"谁先谁后"完全由代码里的 `await` 位
置决定，不存在任意一行代码中间被打断的风险。

### Async Lab 没讲、但你马上会用到的:`asyncio.to_thread()`

Async Lab 明确把这个排除在范围之外，但 `tools.py` 里马上就要写：把一个同步、阻塞的调用（读文件、
跑子进程）丢给一个worker 线程去执行，而不是直接摆在 event loop 所在的这一个线程上跑。

为什么要这么做？因为 event loop 只有一条线程。如果某个协程里直接调用了一个会阻塞好几秒的同步函
数，这几秒钟里 event loop **完全动不了**——不光是当前这个任务卡住，所有其他协程也一起被冻结，包
括正在等模型返回、等其他工具执行的那些。`asyncio.to_thread()` 把这类阻塞调用挪到另一个线程去跑，
主线程上的 event loop 依然能继续调度别的协程——这里"线程"帮上忙，恰恰是因为它在等 I/O，GIL 会被
释放，跟"多线程能不能并行计算"完全是两个问题。

### 所以这门课为什么用 `async`，不是多进程/多线程

`model.py` 要做的事情——发一个 HTTP 请求，等网络那头把 JSON 传回来——绝大部分时间都花在"等"上，
不是在算什么。这正是协程最擅长的场景：不需要额外的进程/线程开销，也不需要真正的多核并行，只需要
"我在等的时候，把机会让给别的等待中的事情"。如果 Phi 要做的是本地跑一个大模型推理这种吃满 CPU 的
活，那答案会完全不同——那才是多进程的地盘。

接下来写 `model.py` 的时候，每一个 `async def` 和 `await`，背后都是刚才这幅地图里的某一处。
