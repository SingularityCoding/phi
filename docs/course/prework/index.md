# 课前准备

Phi 正课会大量用到异步编程：等待 Model 返回要用到它，一点点接收 streaming 数据要用到它，
Harness 处理 timeout 和 cancellation 要用到它，Textual 界面本身也跑在自己的 event loop
里。正课会带你理解这些机制如何服务于 Agent，但不会停下来从头教 Python 异步编程——所以
希望你在进入正课之前，已经对 `async` / `await` 心里有数。

## 先自测，再决定要不要做 Lab

不管你之前写没写过异步代码，都可以先做一遍 [Async Readiness Check](async-lab/readiness-check.md)：

```text
已经熟悉 async / await ───────────────┐
                                      ├─> Async Readiness Check ─> Phi Chapter 01
需要建立异步心智模型 ─> Async Lab ───┘
```

- 所有题目都能讲清楚：直接跳过 Lab，进入正课；
- 有题目讲不清楚：回到对应的 Lab checkpoint 补一下，再回来重新作答。

这份检查是开放资料的自我校验，不计分，也不用提交给任何人——纯粹是帮你自己判断准备好了
没有。

## Async Lab 在讲什么

[Async Lab](async-lab/index.md) 会带你从熟悉的同步 Python 出发，用一个离线的多源文档
检索场景，一步步搭出 coroutine、Task、event loop、结构化并发、streaming、timeout 和
cancellation 的运行模型。

整个过程大约需要 **2–3 小时**。所有模拟数据都是固定的，主线内容不需要联网，也不需要
准备 API key、Textual 或 LLM SDK。

## 通过之后

通过 Readiness Check 后，按照 [环境准备](../appendix/setup.md) 初始化与课程 release 匹配
的 Starter Repository，运行一次离线预检，就可以正式开始正课了。
