# Step 02：Coroutine 与顺序 `await`

现在只做两处看似关键的改动：把 `search()` 和 `collect()` 声明为 `async def`，把
`time.sleep()` 改成 `await asyncio.sleep()`。先不要创建 Tasks。

## `async def` 调用时发生什么

普通函数调用会立刻进入函数体；调用 `async def` 函数只会创建一个 **coroutine object**。
函数体尚未开始执行。

```python
coroutine = search(SOURCES[0], QUERY, EventLog())
print(coroutine)
coroutine.close()
```

`await coroutine` 会在当前 Task 中驱动它；`asyncio.create_task(coroutine)` 则会把它包装成
由 event loop 调度的 Task。Task 留到下一页。

## 先预测

下面的 `collect()` 已经是异步函数，每次搜索也使用 `await asyncio.sleep()`。三个数据源会
并发开始吗？总耗时会缩短吗？

??? success "展开预测答案"

    不会。循环先 `await search(docs)`，整个 `search(docs)` 返回后才进入下一次循环。
    **异步函数不等于并发执行**；顺序 `await` 仍然产生顺序结果，总耗时仍接近 0.24 秒。

## 完整代码：`step_02_sequential_async.py`

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
    results: list[SearchResult] = []
    for source in sources:
        results.extend(await search(source, query, log))
    return results, log


async def main() -> None:
    started_at = time.perf_counter()
    results, event_log = await collect(QUERY)
    elapsed = time.perf_counter() - started_at
    print_report("Step 02 - async but sequential", event_log, results, elapsed)


if __name__ == "__main__":
    asyncio.run(main())
```

## 运行并观察

```bash
uv run python -m phi_async_lab.step_02_sequential_async
```

输出与同步基线具有相同的事件形状：

```text
01 | docs       | started    | How do I cancel async work safely?
02 | docs       | waiting    | 0.12s
03 | docs       | resumed
04 | docs       | completed  | 3 results
05 | issues     | started    | How do I cancel async work safely?
...
12 | notes      | completed  | 2 results

results: 7
elapsed: 0.24s
```

`docs` 在 `await asyncio.sleep()` 处确实把控制权还给了 event loop，但此时没有其他已经创建
的检索 Task。event loop 只能等待计时器就绪，然后恢复同一个调用链。

```text
asyncio.run(main())
        │
        └─ await collect()
                │
                ├─ await search(docs)   ── 等待 ── 返回
                ├─ await search(issues) ── 等待 ── 返回
                └─ await search(notes)  ── 等待 ── 返回
```

## `await` 到底做了什么

当 Task 遇到一个尚未完成的 awaitable 时，Python 保存当前 coroutine 的执行位置，并把
控制权交还 event loop。等待条件完成后，Task 重新进入 ready 状态，event loop 以后会从该
位置恢复它。

可以把 coroutine 想成一张带书签的任务卡：`await` 可能把任务卡放进“等待区”，书签记录
恢复位置。但这个比喻有边界：

- `await` **允许**挂起，不保证每次都切换；已经完成的 awaitable 可以立即返回；
- event loop 不会在任意一行抢走控制权；正在运行的普通 Python 代码必须主动到达可挂起点；
- coroutine 不是线程，也不会自动在另一个 CPU 核心上运行。

显式 `async` / `await` 的价值之一，是让调用链中可能暂停和发生任务交错的位置可见。

## 谁调用 `asyncio.run()`

这个命令行 checkpoint 没有 event loop，所以最外层调用一次 `asyncio.run(main())` 来创建、
运行并关闭 loop。进入异步调用链后，只继续 `await`：

```text
同步程序入口 ── asyncio.run(main()) ── await collect() ── await search()
```

不要在 `collect()` 或 `search()` 中再次调用 `asyncio.run()`。pytest、Textual 等 Host 已经
拥有 event loop，它们内部的业务代码也应接入现有 loop。

## 微实验：只创建 coroutine，不等待

在临时 Python shell 中运行：

```bash
uv run python
```

```python
from phi_async_lab.events import EventLog
from phi_async_lab.scenario import SOURCES
from phi_async_lab.step_02_sequential_async import QUERY, search

coroutine = search(SOURCES[0], QUERY, EventLog())
print(type(coroutine))
coroutine.close()
```

你会看到 `<class 'coroutine'>`，但不会看到任何检索事件，因为函数体从未被驱动。

## 用异步测试观察顺序

```bash
uv run pytest tests/test_steps.py -k sequential_awaits
```

仓库配置了 `asyncio_mode = "auto"`，所以 pytest 会为 `async def test_*` 提供 event loop：

```python
async def test_sequential_awaits_still_finish_before_starting_the_next_source() -> None:
    results, event_log = await collect_sequential_async(QUERY, FAST_SOURCES)

    assert len(results) == 7
```

测试本身也遵循相同规则：pytest 拥有最外层 loop，测试函数使用 `await`，而不是调用
`asyncio.run()`。

[下一步：Tasks 与 event loop →](03-tasks.md)
