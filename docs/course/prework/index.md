# 课前准备

Phi 正课会大量使用异步边界：Model 请求需要等待网络，streaming 通过 async iterator
逐步产生数据，Harness 负责 timeout 与 cancellation，Textual Host 也运行在自己的 event
loop 中。正课会解释这些机制如何服务 Agent，但不会停下来从头教授 Python 异步编程。

## 统一入口与出口

所有学生都以 [Async Readiness Check](async-lab/readiness-check.md) 作为正课准备标准：

```text
已经熟悉 async / await ───────────────┐
                                      ├─> Async Readiness Check ─> Phi Chapter 01
需要建立异步心智模型 ─> Async Lab ───┘
```

- 能完整回答所有核心题：可以跳过 Lab，直接进入正课；
- 有任何一题无法解释：完成对应 Lab checkpoint 后重新作答；
- 检查是开放资料的自我校验，不计分，也不要求在线提交。

## Async Lab

[课前 Async Lab](async-lab/index.md) 使用一个离线的多源开发文档检索器，从学生熟悉的
同步 Python 出发，逐步建立 coroutine、Task、event loop、结构化并发、streaming、timeout
和 cancellation 的运行模型。

预计总时长为 **2–3 小时**。所有模拟数据固定，必修路径不访问真实网络，也不需要 API
key、Textual 或 LLM SDK。

## 其他环境准备

通过 Readiness Check 后，按照 [环境准备](../appendix/setup.md) 初始化与课程 release 匹配
的 Starter Repository，并运行离线预检。
