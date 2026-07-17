# 课程说明

Phi 是一门从头构建 Python Agent Harness 的实践课程。这里的“从头”指实现 Harness，
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
- 阅读并规范化 OpenAI-compatible 请求、响应、streaming 与 Tool Call；
- 实现一个有明确步数上限和停止原因的 Agent loop；
- 区分持久化会话、Conversation View、有限 Context、Events 与 Trace；
- 用 Scripted Model 编写确定、离线、可重复的行为测试；
- 根据 Environment 的最终状态判断 Agent 是否完成工作；
- 识别工具执行、审批、超时和 workspace confinement 的能力边界。

## 前置要求

- 能阅读和编写带类型注解的 Python；
- 熟悉 `async` / `await` 的基本使用；
- 能使用 Git、终端和 pytest；
- 不要求了解 Agent 框架内部实现。

## 课程仓库

| 仓库 | 用途 | 学生是否修改 |
| --- | --- | --- |
| `phi-reference` | 完整实现、教师材料与本课程网站 | 否 |
| `phi-starter` | 学生从 Chapter 01 持续构建的项目 | 是 |

## 每章的完成标准

每个 Chapter 都包含相同的交付结构：

1. 阅读概念与边界说明；
2. 在指定文件中完成实现；
3. 运行本章的离线测试；
4. 检查请求历史、事件或 Environment 结果；
5. 回答一个解释设计取舍的问题。

模型最终说了什么通常不是评分依据。课程更关心控制流是否正确、边界是否可信，以及
Environment 是否到达预期状态。

## 关于真实模型

绝大多数练习使用 Scripted Model，因此不需要联网，也不会因模型措辞变化而随机失败。
真实 LiteLLM Proxy 只用于少量显式的 contract check 和最终体验。

!!! warning "不要提交 virtual key"

    LiteLLM virtual key 只应存在于本地 `.env` 或受控的 secret store 中。不得放进代码、
    测试、Trace、截图或课程作业提交。
