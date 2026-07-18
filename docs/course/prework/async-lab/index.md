# Python Async Lab

如果你已经会写 Python，但 `async` / `await` 在你脑子里还没形成清晰的画面——这个 Lab 就是
为你准备的。你不会从零搭一个项目，而是沿着一组完整、可以直接运行的 checkpoints，一步步
「预测—运行—破坏—解释」，亲眼看看并发到底是怎么发生的。

整个过程大约需要 **2–3 小时**。

!!! tip "已经比较熟悉异步编程？"

    可以先直接做 [Async Readiness Check](readiness-check.md)。如果你能完整解释所有答案，
    就不必逐页过一遍 Lab；哪道题答得不确定，它会直接链接回对应的 checkpoint。

## 做完这个 Lab，你应该能

学完之后，你应该能够：

- 区分普通函数、coroutine 与 `asyncio.Task`；
- 解释 event loop 如何在明确的挂起点调度 Tasks；
- 判断代码是顺序等待还是并发执行；
- 使用 `create_task()` 理解调度，并用 `TaskGroup` 表达任务所有权；
- 识别会阻塞整个 event loop 的同步调用；
- 阅读 `await`、`async for` 与 `async with`；
- 推理 timeout、cancellation 与 `finally` cleanup 的传播路径；
- 判断谁应该调用 `asyncio.run()`；
- 阅读并运行确定性的异步 pytest。

这个 Lab 不追求覆盖整个 `asyncio`。`gather()`、Queue、多数据源流式合并、
`ExceptionGroup`、`to_thread()`、线程和真实网络请求都不在这次的范围里，用不上就先不管，
以后遇到了再学。

## 贯穿全程的例子

整个 Lab 用同一个例子：一个多源开发文档检索器。一次查询会发给三个固定的数据源：

| 数据源 | 模拟内容 | 等待时间 |
| --- | --- | ---: |
| `docs` | Python 文档结果 | 0.12 秒 |
| `issues` | Issue tracker 结果 | 0.08 秒 |
| `notes` | 团队笔记结果 | 0.04 秒 |

这些等待时间只是为了让你能肉眼观察到差异，测试本身不会按精确的毫秒数判断对错，而是看
事件发生的顺序、任务的状态和清理是否到位。

## 拿到代码，跑起来

```bash
git clone https://github.com/SingularityCoding/phi-async-lab.git
cd phi-async-lab
uv sync --locked
uv run pytest
```

项目由 uv 管理，运行时代码只用 Python 3.12 标准库，不依赖任何第三方包。`pytest`、
`pytest-asyncio` 和 Ruff 只是开发时用得上。

完整的 `pyproject.toml` 配置如下（`uv.lock` 由 `uv lock` 自动生成，不需要手工编辑）：

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

每个 `step_*.py` 都是完整、可以独立运行的程序，是你真正要读、要改的地方。三个共享文件
只负责固定数据、事件记录和打印输出，不会替你把并发控制流藏起来——真正决定「谁先跑、谁
等谁」的代码，永远在 `step_*.py` 里明明白白地写着。

## 共享场景：`scenario.py`

```python
# 所有 checkpoint 共用同一个「多源检索」场景和同一份固定数据，
# 这样每一页文档改变的只有并发控制流本身，而不是业务逻辑。
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
    delay: float  # 模拟这个数据源的响应耗时，只用于制造可观察的等待
    results: tuple[ResultTemplate, ...]


# 三个数据源的延迟故意拉开差距（0.12 / 0.08 / 0.04 秒），
# 这样顺序等待与并发等待的耗时差异在肉眼和测试里都足够明显。
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
    # 这是一个普通同步函数：生成结果本身不涉及等待，
    # 「等待」这件事总是由调用它的 checkpoint 代码显式表达（sleep / await sleep）。
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
# 整个 Lab 用同一套「事件日志」记录发生了什么、以及发生的先后顺序。
# 墙钟时间（秒数）只用来建立直觉，真正稳定、可断言的是这里的事件序号和顺序。
from dataclasses import dataclass, field
from enum import StrEnum


class EventKind(StrEnum):
    STARTED = "started"  # 一段工作开始
    WAITING = "waiting"  # 即将进入等待（例如 await 一个 sleep 或 I/O）
    RESUMED = "resumed"  # 等待结束，代码从挂起点恢复执行
    COMPLETED = "completed"  # 工作正常完成
    CANCELLED = "cancelled"  # 收到取消信号（CancelledError）
    CLEANED = "cleaned"  # finally 块完成清理
    TICK = "tick"  # 后台任务（如心跳/进度）的一次周期性输出
    TIMED_OUT = "timed_out"  # 调用者观察到超时
    OBSERVED = "observed"  # 调用者观察到某个结果或异常


@dataclass(frozen=True, slots=True)
class Event:
    sequence: int  # 逻辑发生顺序，不是时间戳——多次运行的耗时会变，顺序不会
    actor: str  # 谁产生了这个事件（某个数据源、caller、heartbeat 等）
    kind: EventKind
    detail: str = ""

    def render(self) -> str:
        suffix = f" | {self.detail}" if self.detail else ""
        return f"{self.sequence:02d} | {self.actor:<10} | {self.kind.value:<10}{suffix}"


@dataclass(slots=True)
class EventLog:
    _events: list[Event] = field(default_factory=list)

    def record(self, actor: str, kind: EventKind, detail: str = "") -> None:
        # 序号按 record() 被调用的顺序自增，因此它反映的是「谁先让出/拿回控制权」，
        # 而不是「谁先被写在代码里」。
        self._events.append(Event(len(self._events) + 1, actor, kind, detail))

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def render(self) -> str:
        return "\n".join(event.render() for event in self._events)
```

事件编号反映的是逻辑发生顺序，不是时间戳。墙钟时间只是帮你建立直觉，真正说了算的是事件
日志和测试里断言的这些顺序关系。

## 共享输出：`reporting.py`

```python
# 统一的输出格式，让每个 checkpoint 的 main() 都不用自己拼报告。
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
    print(f"elapsed: {elapsed:.2f}s")  # 仅供直观感受，判断对错请看 event_log 里的顺序
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
