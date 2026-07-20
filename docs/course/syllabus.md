# 课程说明

Phi 是一门从头构建 Python Agent Harness 的实践课程。这里的"从头"指实现 Harness，
不是训练语言模型，也不是让 Agent 框架替我们拥有控制循环。

## 核心定义

<div class="phi-statement" markdown>
**Agent = Model + Harness**

Model 提出下一步输出或动作；Harness 构建 Context、管理状态、授权并执行工具、处理失败，
并决定 Run 何时停止。
</div>

## 学习目标

完成课程后，学生应该能够：

- 解释 Model、Harness、Environment 与 Host 的职责边界；
- 从零写出一个真实的 Model 边界——把 OpenAI-compatible 协议返回的原始 JSON，转成一份可以信任的
  内部类型；
- 从零写出一个 Tool 边界，区分 unknown tool、invalid arguments 与 handler exception 三种失败；
- 从零写出一个有明确步数上限和停止原因的 Agent Loop，并让它跑通一个真实任务；
- 读懂一份真实、完整的 Agent 实现（Phi 本体），说清楚它比自己手写的版本多做了什么、为什么值得多做；
- 对 Context、Session、Safety、MCP、Hooks/Skills、Multi-agent、Events、Hosts 这些能力，能讲出各自
  解决的真实问题、至少一种主流做法，以及 Phi 具体怎么实现的；
- 判断一个能力"值得现在做"还是"应该主动先不做"，并说出判断依据。

## 前置要求

- 能阅读和编写带类型注解的 Python；
- 能使用 Git、终端和 `uv`；
- 在正课开始前通过 [Async Readiness Check](prework/async-lab/readiness-check.md)；
- 用过至少一种 agentic coding 工具（Claude Code、Codex 之类），知道它"能干什么"，这门课讲的是它
  内部"怎么做到的"。不要求了解任何 Agent 框架的实现细节。

不要求学生事先掌握 `async` / `await`。已有异步编程经验的学生可以直接参加
Async Readiness Check；其他学生先完成约 2–3 小时的
[课前 Async Lab](prework/async-lab/index.md)，再参加同一份检查。

## 课程用到的仓库

| 仓库 | 用途 | 学生是否修改 |
| --- | --- | --- |
| [`phi-async-lab`](https://github.com/SingularityCoding/phi-async-lab) | 课前异步编程实验 | 仅做微实验 |
| `mini-agent` | Chapter 01–03 现场从零构建，全程在这个仓库里手写代码 | 是，从第一行代码开始 |
| `phi`（本仓库） | Chapter 04–12 的阅读对象——一份更完整的参考实现 | 否，只读 |

前三章不发"半成品代码"，就是一个空仓库加 `uv init`；给定的只有环境配置和 CLI 入口这类和教学目标无
关的部分。从第四章开始不再写代码，直接对照读 `phi` 本体的源码。

## 两类章节，两种完成标准

**Chapter 01–03（动手实现）**：

1. 理解这一章要解决的问题和关键接口；
2. 现场手写实现，跑真实的模型/工具调用验证——不是"编译通过就算完成"；
3. 对照 Phi 里的对应实现，看多出来的复杂度在解决什么问题；
4. 回答一道设计取舍的思考题。

**Chapter 04–11（概念 + 对照阅读）**：

1. 从"上一章遗留的问题"出发，理解这一章要解决的真实需求；
2. 了解 1-2 种主流做法；
3. 读 Phi 对应模块的源码，说清楚具体是怎么实现的；
4. 参与讨论，不是标准答案式的思考题。

Chapter 12 是收尾：反过来看，即便是 Phi 这样相对完整的实现，也有一份 `deferred.md` 记录着"考虑过但
主动没做"的能力——读的是判断逻辑，不是代码。

## 关于验证

前三章不用 pytest，也不用 Scripted Model 做自动化测试——每写完一段就直接对着真实的课程 LiteLLM
Proxy 跑一次，用"它有没有真的完成任务"来验证，而不是一套预先写好的断言。这是刻意的选择：这门课的
时间有限，比起搭一套测试基础设施，让大家亲眼看到自己写的代码在跟真实模型对话，反馈更直接。

!!! warning "不要提交 virtual key"

    LiteLLM virtual key 只应存在于本地 `.env` 或受控的 secret store 中。不得放进代码、
    测试、Trace、截图或课程作业提交。
