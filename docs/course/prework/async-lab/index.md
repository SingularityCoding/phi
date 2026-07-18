# Python Async Lab

这个 Lab 面向已经会写 Python、但尚未建立 `async` / `await` 心智模型的学生。你不会从零
实现一个项目，而是沿着一组完整、可运行的 checkpoints 做“预测—运行—破坏—解释”。

预计总时长为 **2–3 小时**。

!!! tip "已经熟悉异步编程？"

    直接完成 [Async Readiness Check](readiness-check.md)。如果你能完整解释所有答案，就不必
    逐页完成 Lab；任何答错或不确定的题都会链接回对应 checkpoint。

## 完成标准

完成后，你应该能够：

- 区分普通函数、coroutine 与 `asyncio.Task`；
- 解释 event loop 如何在明确的挂起点调度 Tasks；
- 判断代码是顺序等待还是并发执行；
- 使用 `create_task()` 理解调度，并用 `TaskGroup` 表达任务所有权；
- 识别会阻塞整个 event loop 的同步调用；
- 阅读 `await`、`async for` 与 `async with`；
- 推理 timeout、cancellation 与 `finally` cleanup 的传播路径；
- 判断谁应该调用 `asyncio.run()`；
- 阅读并运行确定性的异步 pytest。

Lab 不试图覆盖整个 `asyncio`。`gather()`、Queue、多数据源流式合并、`ExceptionGroup`、
`to_thread()`、线程与真实网络都不在必修路径中。

## 贯穿项目

项目是一个多源开发文档检索器。查询会发送给三个固定数据源：

| 数据源 | 模拟内容 | 等待时间 |
| --- | --- | ---: |
| `docs` | Python 文档结果 | 0.12 秒 |
| `issues` | Issue tracker 结果 | 0.08 秒 |
| `notes` | 团队笔记结果 | 0.04 秒 |

等待时间只是帮助观察的演示数据。自动测试不以精确毫秒数判断对错，而是断言事件顺序、
任务状态与清理结果。

## 获取代码

```bash
git clone https://github.com/SingularityCoding/phi-async-lab.git
cd phi-async-lab
uv sync --locked
uv run pytest
```

项目由 uv 管理，运行时代码只使用 Python 3.12 标准库。`pytest`、`pytest-asyncio` 和 Ruff
属于开发依赖。

`pyproject.toml` 完整配置如下。`uv.lock` 由 `uv lock` 生成，不手工编辑：

```toml
[project]
name = "phi-async-lab"
version = "0.1.0"
description = "A deterministic pre-course lab for learning Python async programming before Phi."
readme = "README.md"
authors = [
    { name = "ukeSJTU" }
]
requires-python = ">=3.12"
dependencies = []

[project.scripts]
phi-async-lab = "phi_async_lab:main"

[build-system]
requires = ["uv_build>=0.10.7,<0.11.0"]
build-backend = "uv_build"

[dependency-groups]
dev = [
    "pytest>=9.1.1",
    "pytest-asyncio>=1.4.0",
    "ruff>=0.15.21",
]

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
strict_config = true
strict_markers = true
```

## 代码布局

```text
phi-async-lab/
├── pyproject.toml
├── src/phi_async_lab/
│   ├── events.py
│   ├── reporting.py
│   ├── scenario.py
│   ├── step_01_sync.py
│   ├── step_02_sequential_async.py
│   ├── step_03_tasks.py
│   ├── step_04_task_group.py
│   ├── step_05_blocking.py
│   ├── step_06_async_iteration.py
│   └── step_07_cancellation.py
└── tests/test_steps.py
```

每个 `step_*.py` 都是完整、可独立运行的程序。共享文件只负责固定数据、事件记录和输出；
它们不会替学生隐藏并发控制流。

## 共享场景：`scenario.py`

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SearchResult:
    source: str
    title: str
    snippet: str


@dataclass(frozen=True, slots=True)
class ResultTemplate:
    title: str
    snippet: str


@dataclass(frozen=True, slots=True)
class SourceSpec:
    name: str
    delay: float
    results: tuple[ResultTemplate, ...]


SOURCES = (
    SourceSpec(
        name="docs",
        delay=0.12,
        results=(
            ResultTemplate("Coroutines and Tasks", "Tasks drive coroutines on an event loop."),
            ResultTemplate("Task Cancellation", "Cancellation is observed at an await point."),
            ResultTemplate("Asynchronous Iterators", "async for consumes values over time."),
        ),
    ),
    SourceSpec(
        name="issues",
        delay=0.08,
        results=(
            ResultTemplate("Progress freezes during search", "A blocking call stopped the loop."),
            ResultTemplate("Cancelled search leaked work", "A child task was never awaited."),
        ),
    ),
    SourceSpec(
        name="notes",
        delay=0.04,
        results=(
            ResultTemplate("await is not concurrency", "Sequential awaits still run in order."),
            ResultTemplate("One owner for the loop", "Call asyncio.run only at the outer edge."),
        ),
    ),
)


def materialize(source: SourceSpec, query: str) -> list[SearchResult]:
    return [
        SearchResult(
            source=source.name,
            title=template.title,
            snippet=f"{template.snippet} Query: {query}",
        )
        for template in source.results
    ]
```

## 共享事件日志：`events.py`

```python
from dataclasses import dataclass, field
from enum import StrEnum


class EventKind(StrEnum):
    STARTED = "started"
    WAITING = "waiting"
    RESUMED = "resumed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    CLEANED = "cleaned"
    TICK = "tick"
    TIMED_OUT = "timed_out"
    OBSERVED = "observed"


@dataclass(frozen=True, slots=True)
class Event:
    sequence: int
    actor: str
    kind: EventKind
    detail: str = ""

    def render(self) -> str:
        suffix = f" | {self.detail}" if self.detail else ""
        return f"{self.sequence:02d} | {self.actor:<10} | {self.kind.value:<10}{suffix}"


@dataclass(slots=True)
class EventLog:
    _events: list[Event] = field(default_factory=list)

    def record(self, actor: str, kind: EventKind, detail: str = "") -> None:
        self._events.append(Event(len(self._events) + 1, actor, kind, detail))

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def render(self) -> str:
        return "\n".join(event.render() for event in self._events)
```

事件编号是逻辑顺序，不是时间戳。墙钟时间帮助建立直觉，事件日志和测试才是行为事实。

## 共享输出：`reporting.py`

```python
from collections.abc import Sequence

from phi_async_lab.events import EventLog
from phi_async_lab.scenario import SearchResult


def print_report(
    title: str,
    event_log: EventLog,
    results: Sequence[SearchResult],
    elapsed: float,
) -> None:
    print(title)
    print("=" * len(title))
    print(event_log.render())
    print()
    print(f"results: {len(results)}")
    print(f"elapsed: {elapsed:.2f}s")
```

## 学习路径

1. [同步基线](01-sync-baseline.md)：观察等待时间为什么相加；
2. [Coroutine 与顺序 await](02-coroutines.md)：理解 `async` 不自动产生并发；
3. [Tasks 与 event loop](03-tasks.md)：让三个请求真正交错运行；
4. [结构化并发](04-structured-concurrency.md)：用 `TaskGroup` 表达所有权；
5. [阻塞 event loop](05-blocking.md)：故意用 `time.sleep()` 冻结其他 Task；
6. [Async iterator](06-async-iteration.md)：一边接收流式结果，一边更新进度；
7. [Timeout 与 cancellation](07-cancellation.md)：清理并等待被取消的工作；
8. [Async Readiness Check](readiness-check.md)：验证是否具备进入 Phi 正课的异步基础。

[开始 Step 01 →](01-sync-baseline.md)
