# 环境准备

正式课程开始前，请先通过 Async Readiness Check，再在 `phi-starter` 仓库中完成一次本地
预检。下面的命令展示预期流程；Starter Repository 发布时会提供与课程 release 对应的
最终版本。

## 需要安装

- Python 3.12
- uv
- Git
- 一个支持 Python 的编辑器

检查版本：

```bash
python3 --version
uv --version
git --version
```

## 完成课前 Async Lab

不熟悉 Python 异步编程的学生先克隆公开 Lab：

```bash
git clone https://github.com/SingularityCoding/phi-async-lab.git
cd phi-async-lab
uv sync --locked
uv run pytest
```

按照 [Async Lab 教程](../prework/async-lab/index.md)完成各 checkpoint，最后通过
[Async Readiness Check](../prework/async-lab/readiness-check.md)。已有异步编程经验的学生
可以直接参加检查；如果有任何核心题无法完整解释，再回到对应 checkpoint。

## 初始化 Starter Repository

```bash
git clone <phi-starter-url>
cd phi-starter
uv sync --locked
cp .env.example .env
```

运行离线测试：

```bash
uv run pytest
```

默认测试不需要 API key，也不应访问网络。

## 配置真实模型

只有标记为 live contract 或课堂演示的步骤需要 LiteLLM virtual key：

```dotenv
PHI_BASE_URL=https://your-course-proxy.example/v1
PHI_API_KEY=sk-your-virtual-key
PHI_DEFAULT_MODEL=your-course-model-alias
PHI_REQUEST_TIMEOUT_SECONDS=180
```

!!! danger "保护你的 key"

    不要把真实 key 粘贴进课程网站、聊天、截图、Trace 或 Git commit。提交前运行
    `git status --short`，确认 `.env` 没有进入暂存区。

## 常见问题

### `uv sync --locked` 提示 lockfile 不一致

确认你位于 Starter Repository 根目录，并且没有手工修改 `pyproject.toml` 或 `uv.lock`。

### 离线测试尝试访问网络

这属于课程基础设施问题。停止测试并报告具体 test name；不要通过填入真实 key 绕过它。

### 本地启动成功但 live contract 失败

先检查 base URL、model alias 和 virtual key 是否来自同一套课程配置。不要在错误信息中公开
完整 key。
