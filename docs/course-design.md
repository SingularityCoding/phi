# Phi 课程设计草案

> [!IMPORTANT]
> 本文是早期课程优先的历史草案，不是 Phi Reference 的工程设计依据。当前架构、规格与
> 实现路线见 [`README.md`](README.md)。课程版本将在完整参考实现稳定后重新设计。
>
从头构建一个 Python Agent Harness，并用它理解 Agent 真正由哪些部分组成。

> [!WARNING]
> 本文是课程与项目的临时设计草案。模块边界、教学顺序和最终练习仍会随着参考实现推进而调整；“计划能力”不代表当前代码已经实现。

## 项目目标

Phi 同时是一个可运行的 Agent 项目和一门实践课程。课程不使用 LangChain、Agno 等 Agent 框架替我们实现核心机制，而是直接构建模型边界、工具协议、Agent loop、上下文管理、状态、事件与测试。

这里的“从头构建”指从头实现 Agent Harness，不是从头训练大语言模型。模型通过教师部署的 LiteLLM Proxy 调用。

课程暂定为两节 Python 实践课，每节约 3 小时。当前阶段先不按六小时删减内容，而是完成参考实现、厘清每个模块的职责，再决定：

- 哪些基础设施由教师提前提供；
- 哪些关键代码留给学生实现；
- 哪些能力进入课堂主线；
- 哪些能力作为课后扩展或完整参考实现保留。

## 核心定义

**Agent = Model + Harness**

Model 根据当前输入提出下一步输出或动作；Harness 持有状态、构建上下文、执行控制循环、授权和调用工具，并决定何时停止。UI 是 Agent 的宿主与观察窗口，不属于 Agent 本身。

一个更完整的运行关系是：

> Model proposes. Harness owns authority. Environment provides ground truth.

| 术语 | 暂定含义 |
| --- | --- |
| Model | 接收一次请求并产生一次响应；在课程抽象中视为无状态组件 |
| Harness | 管理状态、上下文、工具、控制循环、权限、停止条件和可观测性 |
| Agent | Model 与 Harness 组成的有界闭环系统 |
| Environment | 文件系统、进程、测试结果和外部服务等可验证的真实环境 |
| State | Harness 持有的规范事实，是一次运行的真实状态 |
| Transcript | 与模型交互所需的完整协议历史 |
| Context | 某次模型调用时，从 State 投影出的有限输入 |
| Trace | 面向开发者的完整事件记录，不等同于发给模型的 Context |
| Memory | 跨运行保存信息的机制，以及相应的读写策略 |

Workflow 与 Agent 也不是同一个概念：Workflow 的下一步主要由代码预先选择；Agent 的下一步允许由模型在 Harness 规定的能力和边界内选择。

## 总体架构

```text
Typer CLI             Textual TUI
    \                     /
     \                   /
      Application Services
               |
            Harness ---------------- Environment
               |
             Model
               |
   OpenAI-compatible HTTP
               |
        LiteLLM Proxy
               |
       Upstream Models
```

Python 代码统一放在 `src/phi/` 命名空间下。核心依赖保持单向：

```text
phi.ui  ->  phi.harness  ->  phi.model
```

- Model 不导入 Harness 或 UI。
- Harness 不导入 Textual，也不把状态所有权交给 UI。
- CLI 和 TUI 只是入口，共用同一组应用服务与 Harness。
- Textual TUI 是观察 Agent 行为的“显微镜”，不是 Agent loop 的拥有者。

## 课程模块草案

以下是当前的能力地图，还不是最终的六小时课表。

### 1. Model 与协议边界

- 完成一次真实模型调用；
- 理解 messages、roles、tools、usage 和 finish reason；
- 将 OpenAI-compatible wire format 转成 Phi 内部类型；
- 用 Scripted Model 完成确定性的离线测试。

### 2. 一次完整的 Tool Round Trip

- 向模型发送工具 schema；
- 解析带 call ID 的结构化 tool call；
- 校验参数、执行 Python 工具；
- 将 tool result 回填后再次调用模型；
- 先手工完成一次往返，再抽象为自动循环。

### 3. Tool Runtime 与能力边界

- Tool、Registry、Dispatcher；
- unknown tool、非法参数和工具异常；
- Harness 注入工作目录等可信参数；
- 文件访问范围、审批与副作用边界。

### 4. Run State 与 Agent Loop

- Run、Step、Model Turn、Tool Call、Tool Result；
- 有界循环和明确的 Run Status；
- 正常完成、最大步数、失败、取消和无进展停止；
- 多个 tool calls 及其结果配对。

### 5. Context Engineering

- 区分 State、Transcript、Context、Trace 和 Memory；
- 上下文预算、裁剪与 compaction；
- 保持 assistant tool call 与 tool result 的原子配对；
- UI 和 Trace 可以保留完整输出，而模型只接收受控投影。

### 6. Failure、Safety 与 Control

- 网络、鉴权、限流和协议错误；
- 模型拒绝、输出截断和未知 finish reason；
- 工作区 confinement、路径与 symlink escape；
- timeout、approval、幂等性和 side-effect uncertainty；
- 默认 fail closed，而不是静默扩大权限。

### 7. Events、Testing 与 Eval

- 用 typed Agent Events 暴露运行过程；
- 同一事件流服务 Textual、headless CLI、pytest 和 JSONL trace；
- Fake/Scripted Model 的确定性行为测试；
- LiteLLM Proxy contract smoke tests；
- 根据环境最终状态评估 Agent，而不是相信模型声称“已完成”。

### 8. 完整 Phi 的扩展能力

这些能力属于最终目标，不会被排除，但会建立在前面的协议、状态和事件边界之上。

| 能力 | 主要归属 |
| --- | --- |
| Agent Skills | 指令、Context 与工具组合的按需加载 |
| MCP | Tool source 与远程调用适配器 |
| Context Compaction | Context 构建策略 |
| Slash Commands | TUI 内的应用命令系统 |
| Session Resume / Fork Tree | 持久化会话与分支状态图 |
| Multi-agent | Agent 组合、委派、fan-out 与 orchestration |
| Memory | 跨运行持久化及读写策略 |
| Verification | 用测试或其他 oracle 验证环境结果 |

## 当前实现焦点：Model

第一阶段从 `phi.model` 开始。计划包含以下角色：

```text
ModelConfig
ModelRequest
ModelResponse
ModelError
Model Protocol
OpenAICompatibleModel
ScriptedModel
ModelEvent
```

Model 层负责：

- endpoint、model alias、virtual key 和 timeout；
- messages、tools 与 generation options 的序列化；
- HTTP/SSE 通信；
- content、tool calls、usage 和 finish reason 的归一化；
- 将网络与协议问题转成明确的模型错误；
- 为测试提供可替换的 Scripted Model。

Model 层暂不负责：

- conversation history 和 context assembly；
- system instructions；
- 工具执行与审批；
- Agent loop 和 retry policy；
- compaction、session、memory；
- TUI 渲染。

可以把边界概括为：

> Model 层负责把远端协议转换成可信事实；Harness 负责依据这些事实作出控制决策。

### LiteLLM Proxy 决策

Phi 不计划安装 LiteLLM Python SDK。LiteLLM 已经作为服务器端 Proxy 负责供应商适配、路由、鉴权、预算与限流；Phi 只实现一个 OpenAI-compatible 客户端，直接向 Proxy 发出 HTTP 请求。

```text
Phi Model types
       |
OpenAI-compatible client
       |
LiteLLM Proxy + virtual key + stable model alias
       |
Upstream model provider
```

这样可以保留真实的 messages、tools、tool calls、stream chunks、usage 和 finish reason，避免客户端与 Proxy 重复进行 LiteLLM 归一化。普通本地工具仍由 Phi Harness 执行，Proxy 只负责传递工具协议。

Model 模块计划按四个能力逐步实现：

1. **Hello Model**：通过虚拟 key 完成一次 `/chat/completions` 调用。
2. **Honest Boundary**：完整保留 content、tool calls、usage 和 finish reason。
3. **Testable Model**：实现能记录请求、脚本化响应与异常的 Scripted Model。
4. **Streaming Model**：解析 SSE，组装文本和 tool-call fragments，并产生 typed events。

随后增加真实 Proxy contract checks，至少覆盖普通回复、streaming、tool call、tool-result round trip、无权限模型和失效 key。

## CLI、TUI 与 Slash Commands

Phi 使用 Typer 提供进程级 CLI，裸执行 `phi` 时启动 Textual TUI。未来可能增加：

```text
phi                              # 启动 Textual TUI
phi run "fix the failing tests"  # 无头运行
phi doctor                       # 检查配置与 Proxy 能力
phi session list
phi session resume <id>
phi session fork <id>
phi eval
```

Typer CLI 与 TUI 内的 slash commands 是两个入口：

```text
Typer CLI ---------+
                   +--> Application Services --> Harness
Slash Commands ----+
```

例如 `phi session fork <id>` 和 `/fork <id>` 应调用同一个会话服务，而不是各自实现一套 fork 逻辑。

## 课程仓库与教学方式

计划提供两个仓库：

- **Starter repository**：学生 fork 后开发，包含 Textual TUI、pytest、配置和其他基础设施，以及有意留下的实现空缺。
- **Private reference repository**：包含完整实现，供教师备课、演示与课后对照。

每个模块都应同时设计三样东西：

1. Reference 中完整、可运行的实现；
2. Starter 中留给学生的最小而关键的代码切口；
3. 能证明协议不变量和行为结果的测试。

确定性测试优先使用 Scripted Model；真实模型只承担少量端到端 contract 或 acceptance checks。测试重点是请求历史形状、事件、停止原因和环境结果，不精确断言模型最终措辞。

课前，学生需要：

1. 提供邮箱并接受 LiteLLM Proxy 邀请；
2. 获得个人 virtual `sk-...` key；
3. 将 `.env.example` 复制为 `.env` 并填写 key 与课程 model alias；
4. 完成一次预检调用。未来由 `phi doctor` 承担这一步。

任何 API key 都不得提交进 Git。

## 当前状态

目前已经具备：

- 使用 `uv` 管理的 Python 3.12 `src` layout；
- `phi` 命名空间包；
- Typer CLI，裸执行时启动 TUI；
- Textual 应用空壳；
- 基于 Pydantic Settings 的 LiteLLM Proxy 配置；
- pytest、Ruff、ty、coverage 和 pre-commit/prek 基础设施；
- CLI 与 Textual 启动测试。

下一步是设计并实现 `phi.model`。

## 本地开发

安装锁定依赖：

```bash
uv sync --locked
```

准备本地配置：

```bash
cp .env.example .env
```

填写以下变量：

```dotenv
PHI_BASE_URL=https://ai.ukehome.top/v1
PHI_API_KEY=sk-your-virtual-key
PHI_DEFAULT_MODEL=your-course-model-alias
PHI_REQUEST_TIMEOUT_SECONDS=180
```

启动当前的 Textual 应用空壳：

```bash
uv run phi
```

运行本地验证：

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

## 预期包结构

包会随能力实现逐步增加，不提前创建空模块：

```text
src/phi/
├── cli/             # Typer 入口与进程级命令
├── model/           # 模型协议、Proxy adapter、Scripted Model
├── harness/         # Run state、Agent loop、events
├── tools/           # Tool protocol、registry、runtime
├── context/         # Context builder、compaction、Agent Skills
├── sessions/        # 持久化、resume、fork tree
├── mcp/             # MCP tool adapter
├── orchestration/   # Multi-agent 与委派
└── ui/              # Textual TUI 与 slash commands
```

测试与源码按模块对应；需要真实 Proxy 的测试应与默认离线测试分开。

## 尚未定稿

- 六小时课程最终如何分节；
- 每个模块哪些代码由教师提供、哪些由学生实现；
- 课堂 capstone 的具体任务和工具集合；
- Tool、Run、Event 与 Session 的最终数据模型；
- sandbox 与审批做到什么安全级别；
- 哪些扩展能力进入课堂，哪些只进入完整 Phi；
- acceptance checks、行为 eval 和评分方式；
- Starter 与 private reference 的发布和同步流程。

Phi 的设计会继续对照 [Gemma](https://github.com/thecarbonlayer/gemma) 等从头实现 Agent Harness 的项目，但不会以复刻某个框架或仓库为课程目标。
