---
hide:
  - navigation
  - toc
---

# Build an Agent from Scratch { .phi-home-title }

<section class="phi-hero">
  <div class="phi-hero__copy">
    <span class="phi-kicker">PYTHON AGENT HARNESS · COURSE PROTOTYPE</span>
    <div class="phi-hero__title" aria-hidden="true">Build an Agent<br>from Scratch</div>
    <p>从一次模型请求开始，逐步构建工具协议、控制循环、上下文、会话与可观测性。
    最终得到一个由自己理解、实现并能够解释的 Agent Harness。</p>
    <p class="phi-hero__actions">
      <a class="md-button md-button--primary" href="prework/async-lab/">完成课前 Async Lab</a>
      <a class="md-button" href="chapters/01-model-boundary/">开始 Chapter 01</a>
      <a class="md-button" href="schedule/">查看课程安排</a>
    </p>
  </div>

  <div class="phi-hero__mark" aria-hidden="true">φ</div>
</section>

<div class="phi-equation" markdown>
<span>Agent</span><span class="phi-equation__operator">=</span><span>Model</span><span class="phi-equation__operator">+</span><span>Harness</span>
</div>

## 这门课会构建什么

<div class="phi-card-grid">
  <article class="phi-card">
    <span class="phi-card__number">01</span>
    <h3>Model Boundary</h3>
    <p>把 OpenAI-compatible wire format 转换为 Phi 内部可信类型，并用 Scripted Model
    获得确定性的测试边界。</p>
    <p><a href="chapters/01-model-boundary/">进入章节 →</a></p>
  </article>

  <article class="phi-card">
    <span class="phi-card__number">02</span>
    <h3>Tool Round Trip</h3>
    <p>让模型提出 Tool Call，由 Harness 完成查找、校验、执行和结果回填。</p>
    <p><a href="chapters/02-tool-round-trip/">进入章节 →</a></p>
  </article>

  <article class="phi-card">
    <span class="phi-card__number">03</span>
    <h3>Agent Loop</h3>
    <p>把一次往返升级为有步数上限、停止原因、事件与失败语义的完整控制循环。</p>
    <p><a href="chapters/03-agent-loop/">进入章节 →</a></p>
  </article>
</div>

## 学习方式

!!! tip "第一次使用 async / await？"

    正课默认学生已经通过 [Async Readiness Check](prework/async-lab/readiness-check.md)。
    如果你有 Python 开发经验但不熟悉异步编程，请先完成约 2–3 小时的
    [课前 Async Lab](prework/async-lab/index.md)。已经熟悉异步编程的学生可以直接挑战
    Readiness Check。

<div class="phi-path" markdown>
  <div class="phi-path__step" markdown><strong>01 · Observe</strong><span>先观察一次完整行为和 Trace。</span></div>
  <div class="phi-path__step" markdown><strong>02 · Explain</strong><span>理解协议、不变量与能力边界。</span></div>
  <div class="phi-path__step" markdown><strong>03 · Implement</strong><span>在 Starter Repository 中完成最小代码切口。</span></div>
  <div class="phi-path__step" markdown><strong>04 · Verify</strong><span>用离线测试验证协议形状和环境结果。</span></div>
</div>

!!! note "这是课程站原型"

    当前页面用于验证课程的信息结构和视觉方向。最终章节、练习切口与测试会从稳定的
    Phi Reference release 派生，不反向约束正在实现的 Reference 架构。
