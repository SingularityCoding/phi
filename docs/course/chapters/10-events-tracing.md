# Chapter 10 · Events & Tracing

<div class="phi-chapter-meta" markdown>
<span>概念讲解</span><span>不现场写代码</span><span>对照 phi/harness/events.py、phi/sessions/trace.py</span>
</div>

mini-agent 唯一的可观测手段是 `print()`。你自己盯着终端跑一次任务,这就够用了。但只要出现下面任何
一种情况,`print()` 就不够了:任务跑完之后想回头排查到底发生了什么;同一次运行需要同时喂给好几种不
同的界面展示;或者你需要拿出证据证明某次运行确实做了什么、没做什么。这一章讲的是:怎么把"发生了什
么"变成一份结构化的、可以被复用和验证的记录,而不是一堆滚动过去就没了的文本。

## 观察行为和影响行为要分开

一旦你开始往循环里加"通知外部发生了什么"的机制,很容易顺手也让这个机制去改变行为——比如某个监听
者判断"这一步有问题"就直接改写了结果。这是一个危险的混淆:观察不应该有副作用。上一章讲的 Hook 才
是允许改变行为的机制,这一章的 Event 只负责如实通知。

## 主流做法

- **print 式调试**:我们目前一直在用的方式,足够简单,但没有结构、无法回放、无法多路复用。
- **结构化 JSON 日志**:每条记录是结构化数据而不是自由文本,更适合程序化处理。
- **分布式追踪系统**:OpenTelemetry 这类 span 式追踪,擅长跨服务的调用链路,但概念模型更重。
- **专门的 Agent 评测/可观测性平台**:面向 Agent 场景做了专门优化,通常是外部托管服务。

## Phi 怎么做

打开 `phi/src/phi/harness/events.py` 和 `phi/src/phi/sessions/trace.py`。

- **封闭的一组类型化 Event,而不是自由字符串**:`RunStarted`、`ModelCallStarted`、
  `ModelCallDelta`、`ModelCallCompleted`、`ToolCallStarted`、`ToolCallCompleted`、
  `ApprovalDecided`、`RunFinished`——这几个 dataclass 就是全部的事件类型。所有消费者(TUI、CLI、测
  试、落盘的 Trace)订阅的是同一条类型化的流,新增一种观察方式,不需要改动循环本身的代码。
- **事件内容在创建时就冻结**:每个事件的数据在生成的那一刻就被固定下来,不会因为之后某处共享状态
  被修改,而让一个已经发出去的事件内容跟着悄悄变化。
- **一个监听者的异常不传染**:`EventBus.emit` 按订阅顺序把事件送给每个监听者,并且把异常隔离在单个
  监听者内部——一个写坏的监听者不会让整次 Run 崩掉,也不会挡住其他监听者收到事件;但真正的取消
  (cancellation)信号仍然会照常传播,不会被这层隔离吞掉。
- **落盘的 Trace 会主动脱敏**:`sessions/trace.py` 里的 `TraceWriter` 把这条事件流持久化成 JSONL,
  在写入磁盘之前,会用正则主动识别并遮蔽看起来像凭证的内容——Bearer token、`sk-` 开头的 API key,
  以及任何键名形如 `api_key`/`authorization`/`cookie`/`password`/`secret`/`token` 的键值对。这不
  是写在文档里提醒开发者"别记录密钥"的一条规则,而是真正在代码里执行的检查。

## 讨论

Event 的数据在创建时就被冻结,这个设计防住的是"事件发出后被悄悄改写"。那如果问题出在更早的一
步——事件本身在创建时读到的就是一个已经被污染或者过期的状态,冻结机制还能保护什么吗?你觉得这种更
早阶段的问题,应该在哪一层被拦住?
