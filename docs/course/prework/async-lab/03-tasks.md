# Step 03：Tasks 与 event loop

上一页只有一条检索调用链。现在先为三个 coroutine 分别创建 Task，再等待结果。

## Coroutine 与 Task 不是同一个东西

- coroutine object 保存一段尚未完成的异步执行；
- Task 持有一个 coroutine，并把它登记给当前 event loop 调度；
- `create_task()` 返回后，Task 已经被安排运行，但当前代码要先把控制权交还 event loop；
- `await task` 等待该 Task 的结果，也给 event loop 运行其他 ready Tasks 的机会。

## 先预测

下面的代码最后仍然按列表顺序 `await task`。这是否会让请求重新变成顺序执行？

??? success "展开预测答案"

    不会。三个 Tasks 在进入等待结果的循环前已经全部创建。当代码 `await` 第一个 Task 时，
    另外两个 Tasks 也可以在同一个 event loop 上推进。结果按输入顺序收集，**完成顺序**却由
    每个数据源何时就绪决定。

## 完整代码：`step_03_tasks.py`

```python
import asyncio
import time
from collections.abc import Sequence

from phi_async_lab.events import EventKind, EventLog
from phi_async_lab.reporting import print_report
from phi_async_lab.scenario import SOURCES, SearchResult, SourceSpec, materialize

QUERY = "How do I cancel async work safely?"


async def search(source: SourceSpec, query: str, event_log: EventLog) -> list[SearchResult]:
    event_log.record(source.name, EventKind.STARTED, query)
    event_log.record(source.name, EventKind.WAITING, f"{source.delay:.2f}s")
    await asyncio.sleep(source.delay)
    event_log.record(source.name, EventKind.RESUMED)
    results = materialize(source, query)
    event_log.record(source.name, EventKind.COMPLETED, f"{len(results)} results")
    return results


async def collect(
    query: str,
    sources: Sequence[SourceSpec] = SOURCES,
    event_log: EventLog | None = None,
) -> tuple[list[SearchResult], EventLog]:
    log = event_log or EventLog()
    tasks = [
        asyncio.create_task(search(source, query, log), name=f"search:{source.name}")
        for source in sources
    ]

    results: list[SearchResult] = []
    for task in tasks:
        results.extend(await task)
    return results, log


async def main() -> None:
    started_at = time.perf_counter()
    results, event_log = await collect(QUERY)
    elapsed = time.perf_counter() - started_at
    print_report("Step 03 - explicit tasks", event_log, results, elapsed)


if __name__ == "__main__":
    asyncio.run(main())
```

## 运行并观察

```bash
uv run python -m phi_async_lab.step_03_tasks
```

```text
01 | docs       | started    | How do I cancel async work safely?
02 | docs       | waiting    | 0.12s
03 | issues     | started    | How do I cancel async work safely?
04 | issues     | waiting    | 0.08s
05 | notes      | started    | How do I cancel async work safely?
06 | notes      | waiting    | 0.04s
07 | notes      | resumed
08 | notes      | completed  | 2 results
09 | issues     | resumed
10 | issues     | completed  | 2 results
11 | docs       | resumed
12 | docs       | completed  | 3 results

results: 7
elapsed: 0.12s
```

三个 `started` 都发生在第一个 `completed` 之前。总耗时从接近延迟之和变成接近最大延迟。
这就是 I/O concurrency：同一个线程没有同时执行三段 Python 代码，而是在一个 Task 等待时
推进另一个 ready Task。

```text
当前 Task 创建三个子 Task
        │
        ├─ docs   ── await sleep(0.12) ───────────────> complete
        ├─ issues ── await sleep(0.08) ────────> complete
        └─ notes  ── await sleep(0.04) ─> complete
                         event loop
```

## 为什么 `create_task()` 后不会立即插入执行

```python
task = asyncio.create_task(search(...))
print("caller still running")
```

`create_task()` 安排新 Task，但 event loop 不会抢占当前这段同步 Python。通常要等当前 Task
到达 `await`、返回或其他可挂起边界，新 Task 才获得运行机会。这种 cooperative scheduling
与操作系统可以随时抢占线程不同。

## 微实验：把创建与等待重新混在一起

临时把 `collect()` 改成：

```python
results: list[SearchResult] = []
for source in sources:
    task = asyncio.create_task(search(source, query, log))
    results.extend(await task)
```

先预测，再运行。虽然使用了 Task，但每次创建后立刻等待它完成，下一个 Task 还不存在，事件
形状会退回 Step 02。**使用 Task 这个类型不自动保证并发；任务的创建与等待结构才决定
并发。**

完成后恢复文件：

```bash
git restore src/phi_async_lab/step_03_tasks.py
```

## 用测试验证并发事实

```bash
uv run pytest tests/test_steps.py -k explicit_tasks
```

测试不要求精确完成顺序，也不使用耗时阈值。它只验证：最后一个 `started` 早于第一个
`completed`。

```python
last_start = max(
    event_index(event_log.events, source.name, EventKind.STARTED) for source in FAST_SOURCES
)
first_completion = min(
    event_index(event_log.events, source.name, EventKind.COMPLETED) for source in FAST_SOURCES
)
assert last_start < first_completion
```

## 当前代码留下的问题

正常路径会逐一等待所有 Tasks，但如果中途发生异常，手工维护的列表需要自己保证其余任务
被取消和等待。Task 不是“发射后不用管”的后台工作。下一页用结构化并发表达所有权。

[下一步：结构化并发与 TaskGroup →](04-structured-concurrency.md)
