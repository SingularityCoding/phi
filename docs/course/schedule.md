# 课程安排

正课前，所有学生都需要通过 [Async Readiness Check](prework/async-lab/readiness-check.md)。
已有异步编程经验的学生可以直接参加检查；其他学生先完成
[课前 Async Lab](prework/async-lab/index.md)。

这份安排按内容顺序组织，不按课时切分——具体怎么分成几次课、每次多久，取决于实际授课节奏，这里不
预设。顺序本身是有依据的：前三章现场手写，把最核心的"模型边界 → 工具边界 → Agent Loop"跑通一遍；
从第四章开始不再写代码，按"跑通的循环接下来会遇到什么问题"这个逻辑，一章一章带出 Context、
Session、Safety 等能力，每章都读 Phi 对应模块的真实实现；最后一章反过来，看看即便是 Phi 也主动选
择不做的事。

| Chapter | 主题 | 形式 | 对照的 Phi 模块 |
| --- | --- | --- | --- |
| — | Python Async Lab（课前） | 自己动手 | — |
| [00](chapters/00-orientation.md) | 开场：Phi 全景演示 + Python 并发回顾 | 演示 + 回顾 | — |
| [01](chapters/01-model-boundary.md) | Model 边界 | 现场手写 | `phi/model/` |
| [02](chapters/02-tool-boundary.md) | Tool 边界 | 现场手写 | `phi/tools/` |
| [03](chapters/03-agent-loop.md) | Agent Loop | 现场手写 | `phi/harness/run.py` |
| [04](chapters/04-context-engineering.md) | Context Engineering | 概念 + 对照阅读 | `phi/harness/compaction.py` |
| [05](chapters/05-sessions.md) | Session 持久化与分支 | 概念 + 对照阅读 | `phi/sessions/` |
| [06](chapters/06-safety-approval.md) | Safety & Approval | 概念 + 对照阅读 | `phi/tools/approval.py`、`phi/environment/` |
| [07](chapters/07-mcp.md) | MCP | 概念 + 对照阅读 | `phi/mcp/` |
| [08](chapters/08-hooks-skills.md) | Hooks & Skills | 概念 + 对照阅读 | `phi/harness/hooks.py`、`phi/skills/` |
| [09](chapters/09-multi-agent.md) | Multi-agent | 概念 + 对照阅读 | `phi/agents/` |
| [10](chapters/10-events-tracing.md) | Events & Tracing | 概念 + 对照阅读 | `phi/harness/events.py`、`phi/sessions/trace.py` |
| [11](chapters/11-hosts.md) | Hosts | 概念 + 对照阅读 | `phi/cli/`、`phi/ui/` |
| [12](chapters/12-industry-frontier.md) | 工业界 Agent 还差什么 | 讨论收尾 | `phi/docs/deferred.md` |

## 课程版本原则

章节内容固定到一个稳定的 Phi release。Phi 本体后续演进不会悄悄改变正在进行中的课程：

```text
Phi release
    │
    ├── 课程站内容（04–12 章对照阅读的对象）
    └── mini-agent 仓库（01–03 章现场构建的对象）
```

课程网站和 `mini-agent` 仓库会明确标出它们对应的 Phi release。
