# 课程安排

下面是课程站原型使用的章节节奏。最终授课时长和课堂切口会在 Phi Reference 稳定后确定。

| 阶段 | Chapter | 主题 | 主要产出 |
| --- | --- | --- | --- |
| Foundations | [01](chapters/01-model-boundary.md) | Model Boundary | 归一化类型、Model Protocol、Scripted Model |
| Tools | [02](chapters/02-tool-round-trip.md) | Tool Round Trip | Registry、参数校验、Dispatcher、Tool Result |
| Harness | [03](chapters/03-agent-loop.md) | Agent Loop | Run、Step、Events、停止原因 |
| State | 04 | Sessions & Context | Conversation View、Context projection、compaction |
| Safety | 05 | Environment & Approval | confinement、timeout、approval、failure policy |
| Integration | 06 | Hosts & Runtime | Typer CLI、Textual TUI、shared services |
| Capstone | 07 | Complete Agent | 端到端任务、Trace 与 Environment eval |

## 一个 Chapter 的课堂节奏

<div class="phi-timeline" markdown>
  <div markdown><span>00–20 min</span><strong>观察与拆解</strong><p>运行已经准备好的行为样例，先建立完整心智模型。</p></div>
  <div markdown><span>20–50 min</span><strong>协议与不变量</strong><p>阅读输入输出形状，讨论失败时 Harness 应如何决策。</p></div>
  <div markdown><span>50–110 min</span><strong>实现</strong><p>完成一个小而关键的代码切口，持续运行本章测试。</p></div>
  <div markdown><span>110–130 min</span><strong>故障实验</strong><p>注入非法响应、未知工具或步数耗尽，观察系统行为。</p></div>
  <div markdown><span>130–150 min</span><strong>回顾</strong><p>解释边界、记录取舍，并为下一章整理代码。</p></div>
</div>

## 课程版本原则

课程章节固定到一个稳定的 Phi release。Reference 后续演进不会悄悄改变正在进行中的课程：

```text
Phi Reference release
        │
        ├── Course site version
        └── Starter repository version
```

课程网站和 Starter Repository 会明确标出它们共同对应的 release。
