# Chapters

课程分两类:前三章现场从零实现一个最小 Agent(项目代号 `mini-agent`,不带 streaming、不带 TUI、不
连 MCP,只有 Model + Tool + Loop 三块最核心的东西);从第四章开始,不再现场写代码,而是用"这个能力
要解决什么问题 → 主流做法是什么 → Phi 具体怎么实现"这个结构,带大家读真实 Phi 的对应模块。最后一章
反过来,看看即便是完整的 Phi,还刻意没做哪些事、为什么。

<div class="phi-chapter-list" markdown>
  <a class="phi-chapter-row" href="00-orientation/">
    <span class="phi-chapter-row__number">00</span>
    <span><strong>开场</strong><small>Phi 全景演示 + Python 并发回顾</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="01-model-boundary/">
    <span class="phi-chapter-row__number">01</span>
    <span><strong>Model 边界</strong><small>动手实现 · 一次可信的模型请求</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="02-tool-boundary/">
    <span class="phi-chapter-row__number">02</span>
    <span><strong>Tool 边界</strong><small>动手实现 · 模型提议，程序执行</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="03-agent-loop/">
    <span class="phi-chapter-row__number">03</span>
    <span><strong>Agent Loop</strong><small>动手实现 · 把两块接成一个能自己干活的循环</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="04-context-engineering/">
    <span class="phi-chapter-row__number">04</span>
    <span><strong>Context Engineering</strong><small>概念 + 对照 · 历史变长之后怎么办</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="05-sessions/">
    <span class="phi-chapter-row__number">05</span>
    <span><strong>Session 持久化与分支</strong><small>概念 + 对照 · 关掉之后还能回来</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="06-safety-approval/">
    <span class="phi-chapter-row__number">06</span>
    <span><strong>Safety &amp; Approval</strong><small>概念 + 对照 · 它会不会把我文件删了</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="07-mcp/">
    <span class="phi-chapter-row__number">07</span>
    <span><strong>MCP</strong><small>概念 + 对照 · 内置工具不够用怎么办</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="08-hooks-skills/">
    <span class="phi-chapter-row__number">08</span>
    <span><strong>Hooks &amp; Skills</strong><small>概念 + 对照 · 不改源码也能定制行为</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="09-multi-agent/">
    <span class="phi-chapter-row__number">09</span>
    <span><strong>Multi-agent</strong><small>概念 + 对照 · 把任务拆给别的 Agent</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="10-events-tracing/">
    <span class="phi-chapter-row__number">10</span>
    <span><strong>Events &amp; Tracing</strong><small>概念 + 对照 · 怎么知道它到底做了什么</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="11-hosts/">
    <span class="phi-chapter-row__number">11</span>
    <span><strong>Hosts</strong><small>概念 + 对照 · 同一个 Agent，不同的界面</small></span>
    <span aria-hidden="true">→</span>
  </a>
  <a class="phi-chapter-row" href="12-industry-frontier/">
    <span class="phi-chapter-row__number">12</span>
    <span><strong>工业界 Agent 还差什么</strong><small>讨论收尾 · 对照 deferred.md</small></span>
    <span aria-hidden="true">→</span>
  </a>
</div>

!!! info

    04-11 章不提供代码切口，重点是读 Phi 对应模块的源码、理解它在解决什么真实问题。12 章更进一
    步：即便是这样一个相对完整的实现，也有一份文档专门记录"考虑过但没做"的能力和判断标准。
