# Step 02：Coroutine 与顺序 `await`

这一页只做两处改动，但很关键：把 `search()` 和 `collect()` 声明为 `async def`，把
`time.sleep()` 换成 `await asyncio.sleep()`。先别急着创建 Task，一步一步来。

## 调用 `async def` 函数时，到底发生了什么

调用普通函数会立刻进入函数体执行；但调用一个 `async def` 函数，得到的只是一个
**coroutine object**——函数体其实还没开始跑。这一点很反直觉，值得停下来看看：

```python
coroutine = search(SOURCES[0], QUERY, EventLog())
print(coroutine)
coroutine.close()
```

`await coroutine` 会在当前 Task 里驱动它往前走；`asyncio.create_task(coroutine)` 则会把它
包了一层，交给 event loop 去调度成一个 Task。Task 到底是什么，我们放到下一页细说。

## 先预测

下面的 `collect()` 已经是异步函数，每次搜索也使用 `await asyncio.sleep()`。三个数据源会
并发开始吗？总耗时会缩短吗？

??? success "展开预测答案"

    不会。循环先 `await search(docs)`，整个 `search(docs)` 返回后才进入下一次循环。
    **异步函数不等于并发执行**；顺序 `await` 仍然产生顺序结果，总耗时仍接近 0.24 秒。

## 完整代码：`step_02_sequential_async.py`

```python
# Step 02：加上了 async/await，但还没有并发——这一步专门用来打破
# 「函数标了 async 就会自动并发」这个常见误解。
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
    # 换成 await asyncio.sleep()：这里会把控制权交还 event loop，
    # 但此刻还没有其他 Task 存在，所以没人能利用这段空隙。
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
    # 依旧是顺序 await：本次 search() 完整返回后，下一次循环才会调用下一个 search()。
    for source in sources:
        results.extend(await search(source, query, log))
    return results, log


async def main() -> None:
    started_at = time.perf_counter()
    results, event_log = await collect(QUERY)
    elapsed = time.perf_counter() - started_at
    print_report("Step 02 - async but sequential", event_log, results, elapsed)


if __name__ == "__main__":
    # 只有最外层、还没有 event loop 的入口才调用 asyncio.run()。
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
