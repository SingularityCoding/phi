# Step 07：Timeout、cancellation 与 cleanup

并发工作的完成路径必须是有界的。调用者可能设置 deadline，用户也可能主动取消。正确的
异步代码不能只“发出取消信号”，还要等待被取消的 Task 完成清理，并让调用者看见真实终止
状态。

## Cancellation 是请求，不是瞬间终止

`task.cancel()` 请求向 Task 注入 `CancelledError`。Task 通常在下一次可挂起点观察到它：

```text
caller: task.cancel()
          │
          └─> Task 在 await 处收到 CancelledError
                    ├─ except：记录取消，然后 re-raise
                    ├─ finally：清理资源
                    └─ caller: await task 后观察到 CancelledError
```

若 Task 正在执行不会交还控制权的同步代码，event loop 不能在任意一行安全地停止它。

## 完整代码：`step_07_cancellation.py`

```python
import asyncio
from collections.abc import Sequence

from phi_async_lab.events import EventKind, EventLog
from phi_async_lab.scenario import SOURCES, SearchResult, SourceSpec, materialize

QUERY = "How do I cancel async work safely?"


async def search(source: SourceSpec, query: str, event_log: EventLog) -> list[SearchResult]:
    event_log.record(source.name, EventKind.STARTED)
    try:
        event_log.record(source.name, EventKind.WAITING, f"{source.delay:.2f}s")
        await asyncio.sleep(source.delay)
        event_log.record(source.name, EventKind.RESUMED)
        results = materialize(source, query)
        event_log.record(source.name, EventKind.COMPLETED, f"{len(results)} results")
        return results
    except asyncio.CancelledError:
        event_log.record(source.name, EventKind.CANCELLED)
        raise
    finally:
        event_log.record(source.name, EventKind.CLEANED)


async def collect_with_deadline(
    query: str,
    timeout_seconds: float,
    sources: Sequence[SourceSpec] = SOURCES,
    event_log: EventLog | None = None,
) -> tuple[list[SearchResult], EventLog]:
    log = event_log or EventLog()
    tasks: list[asyncio.Task[list[SearchResult]]] = []

    async with asyncio.timeout(timeout_seconds):
        async with asyncio.TaskGroup() as task_group:
            for source in sources:
                tasks.append(task_group.create_task(search(source, query, log)))

    results = [result for task in tasks for result in task.result()]
    return results, log


async def run_deadline_demo(
    timeout_seconds: float = 0.06,
    sources: Sequence[SourceSpec] = SOURCES,
) -> EventLog:
    event_log = EventLog()
    try:
        await collect_with_deadline(QUERY, timeout_seconds, sources, event_log)
    except TimeoutError:
        event_log.record("caller", EventKind.TIMED_OUT)
    return event_log


async def run_explicit_cancel_demo(source: SourceSpec = SOURCES[0]) -> EventLog:
    event_log = EventLog()
    task = asyncio.create_task(search(source, QUERY, event_log), name="search-to-cancel")

    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        event_log.record("caller", EventKind.OBSERVED, "CancelledError")

    return event_log


async def main() -> None:
    deadline_log = await run_deadline_demo()
    cancel_log = await run_explicit_cancel_demo()

    print("Step 07A - timeout cancels unfinished children")
    print("================================================")
    print(deadline_log.render())
    print()
    print("Step 07B - cancel is a request that must be awaited")
    print("====================================================")
    print(cancel_log.render())


if __name__ == "__main__":
    asyncio.run(main())
```

## 运行 deadline 场景

```bash
uv run python -m phi_async_lab.step_07_cancellation
```

deadline 是 0.06 秒，因此 `notes` 正常完成，另外两个数据源仍在等待：

```text
01 | docs       | started
02 | docs       | waiting    | 0.12s
03 | issues     | started
04 | issues     | waiting    | 0.08s
05 | notes      | started
06 | notes      | waiting    | 0.04s
07 | notes      | resumed
08 | notes      | completed  | 2 results
09 | notes      | cleaned
10 | docs       | cancelled
11 | docs       | cleaned
12 | issues     | cancelled
13 | issues     | cleaned
14 | caller     | timed_out
```

`asyncio.timeout()` 是 async context manager。deadline 到期时，它通过 cancellation 中断当前
等待；嵌套的 `TaskGroup` 取消并等待未完成子任务。退出 timeout 作用域后，调用者看到的是
`TimeoutError`。因此 `timed_out` 一定记录在子任务 cleanup 之后。

## 运行显式取消场景

第二段日志中，调用者创建 Task、稍作等待、调用 `cancel()`，然后继续 `await task`：

```text
01 | docs       | started
02 | docs       | waiting    | 0.12s
03 | docs       | cancelled
04 | docs       | cleaned
05 | caller     | observed   | CancelledError
```

关键不是第 3 行出现得有多快，而是顺序不变量：

```text
cancelled < cleaned < caller observed cancellation
```

`except asyncio.CancelledError` 记录事实后立即 `raise`。如果把它吞掉并返回普通结果，调用者
会误以为工作成功完成。`finally` 无论成功、失败还是取消都会执行，适合释放当前 coroutine
拥有的资源。

## 微实验：错误地吞掉 cancellation

临时删除 `search()` 中的 `raise`，改成返回空列表：

```python
except asyncio.CancelledError:
    event_log.record(source.name, EventKind.CANCELLED)
    return []
```

再次运行显式取消场景。调用者将不再观察到 `CancelledError`；Task 表面上像正常返回。这会
破坏上层停止语义，也是 Agent Harness 不能接受的行为。

```bash
git restore src/phi_async_lab/step_07_cancellation.py
```

## 用测试验证生命周期

```bash
uv run pytest tests/test_steps.py -k "timeout or cancellation"
```

测试使用差距足够大的受控延迟，但不比较精确毫秒数。它断言：

- deadline 场景中每个已经开始的慢数据源都记录 `cancelled` 和 `cleaned`；
- `timed_out` 是调用者最后观察到的终止事件；
- 显式取消中 cleanup 先于调用者观察异常；
- 名为 `search-to-cancel` 的 Task 没有遗留在 event loop 中。

## 迁移到 Phi

Lab 不是 Agent，也没有提前引入 Model、Harness 或 Textual，但异步关系可以直接迁移：

| Async Lab | Phi 中的对应问题 |
| --- | --- |
| 等待数据源 | 等待 Model、Tool 或外部服务 I/O |
| async iterator | 消费 Model streaming chunks |
| progress Task | Host 在 Run 进行时继续响应和渲染 |
| deadline | Harness 为有界操作设置 timeout |
| `task.cancel()` 后继续等待 | Host 请求取消 Run，并等待其资源清理 |
| EventLog 测试 | 用 Events 与最终状态验证控制流 |

这只是运行结构的映射，不代表数据源是 Model，也不代表 UI 拥有 Harness 控制循环。正课会继续
使用 Phi 的规范领域语言。

[进入 Async Readiness Check →](readiness-check.md)
