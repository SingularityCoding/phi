---
hide:
  - navigation
  - toc
---

# Build an Agent from Scratch { .phi-home-title }

<section class="phi-hero">
  <div class="phi-hero__copy">
    <span class="phi-kicker">PYTHON AGENT HARNESS · 实践课程</span>
    <div class="phi-hero__title" aria-hidden="true">Build an Agent<br>from Scratch</div>
    <p>先从零手写一个最小的 Agent——只有模型边界、工具边界、一个有界循环，不带 streaming、不带界
    面。跑通之后，再去读一个真正完整的实现，看看"完整"这两个字具体多出了什么、每一处为什么值得多
    出这些复杂度。</p>
    <p class="phi-hero__actions">
      <a class="md-button md-button--primary" href="prework/async-lab/">完成课前 Async Lab</a>
      <a class="md-button" href="chapters/01-model-boundary/">开始 Chapter 01</a>
      <a class="md-button" href="chapters/">查看全部章节</a>
    </p>
  </div>

  <div class="phi-hero__mark" aria-hidden="true">φ</div>
</section>

<div class="phi-equation" markdown>
<span>Agent</span><span class="phi-equation__operator">=</span><span>Model</span><span class="phi-equation__operator">+</span><span>Harness</span>
</div>

## 这门课分两半

<div class="phi-card-grid">
  <article class="phi-card">
    <span class="phi-card__number">01–03</span>
    <h3>动手实现 mini-agent</h3>
    <p>模型边界、工具边界、Agent Loop——现场从 <code>uv init</code> 开始，手写一个能跑真实任务
    的最小 Agent。</p>
    <p><a href="chapters/01-model-boundary/">进入 Chapter 01 →</a></p>
  </article>

  <article class="phi-card">
    <span class="phi-card__number">04–11</span>
    <h3>对照阅读真实 Phi</h3>
    <p>不再写代码。每章从一个真实问题出发，先看主流做法，再读 Phi 的实现——Context、Session、
    Safety、MCP、Hooks/Skills、Multi-agent、Events、Hosts。</p>
    <p><a href="chapters/04-context-engineering/">进入 Chapter 04 →</a></p>
  </article>

  <article class="phi-card">
    <span class="phi-card__number">12</span>
    <h3>工业界 Agent 还差什么</h3>
    <p>即便是这样一个相对完整的实现，也有一份文档专门记录"考虑过但没做"的能力，以及为什么。</p>
    <p><a href="chapters/12-industry-frontier/">进入 Chapter 12 →</a></p>
  </article>
</div>

## 学习方式

!!! tip "第一次使用 async / await？"

    正课默认学生已经通过 [Async Readiness Check](prework/async-lab/readiness-check.md)。
    如果你有 Python 开发经验但不熟悉异步编程，请先完成约 2–3 小时的
    [课前 Async Lab](prework/async-lab/index.md)。已经熟悉异步编程的学生可以直接挑战
    Readiness Check。

<div class="phi-path" markdown>
  <div class="phi-path__step" markdown><strong>01 · 现场手写</strong><span>基础三章不给现成代码，跟着一起从零敲出来。</span></div>
  <div class="phi-path__step" markdown><strong>02 · 跑起来</strong><span>每一步都打真实的模型/工具接口验证，不是编译过就算数。</span></div>
  <div class="phi-path__step" markdown><strong>03 · 对照阅读</strong><span>拿自己写的东西，对照真实 Phi 的对应实现，看多出来的复杂度在解决什么问题。</span></div>
  <div class="phi-path__step" markdown><strong>04 · 讨论边界</strong><span>最后一章反过来：完整实现里，哪些东西是主动选择不做的。</span></div>
</div>
