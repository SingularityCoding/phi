# Chapters

课程按 Harness 能力出现的依赖顺序组织。每一章都会落下一个可运行、可测试的纵向能力，
而不是提前创建一棵空的包结构。

<div class="phi-chapter-list" markdown>
  <a class="phi-chapter-row" href="01-model-boundary/">
    <span class="phi-chapter-row__number">01</span>
    <span><strong>Model Boundary</strong><small>一次无状态请求，以及可信的内部响应类型</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="02-tool-round-trip/">
    <span class="phi-chapter-row__number">02</span>
    <span><strong>Tool Round Trip</strong><small>模型提出动作，Harness 持有执行权</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="03-agent-loop/">
    <span class="phi-chapter-row__number">03</span>
    <span><strong>Agent Loop</strong><small>有界、可观察并具有明确停止语义的控制循环</small></span>
    <span aria-hidden="true">→</span>
  </a>
</div>

## 后续能力地图

| Chapter | 能力 | 为什么排在这里 |
| --- | --- | --- |
| 04 | Sessions & Context | 有 Run 之后，才能清楚区分运行内状态与持久化历史 |
| 05 | Safety & Failure | 在真实工具和完整循环上讨论审批与不确定副作用 |
| 06 | Hosts | CLI 与 TUI 复用已经稳定的 Harness service |
| 07 | Capstone | 用 Environment state 评估完整 Agent 结果 |

!!! info

    本页目前只展开前三个章节，用于试验课程网站。后续内容不会在 Reference capability
    实现之前提前承诺具体代码结构。
