# Step 06：用 `async for` 消费流式结果

前面几页的 `search()` 都是等整个数据源处理完，一次性把列表返回给你。但现实中的 I/O 经常
是分批到达的——LLM 的 streaming 响应也不会等全部内容生成完才一次性交付。Async iterator
就是用来让调用者可以一批一批地等「下一份」数据。

这一页只让 `docs` 这一个数据源以流式方式返回结果，先建立好这个心智模型。多个生产者合并、
Queue、背压这些更复杂的话题不在这次的必修范围内。

## 先预测

`consume()` 和 `stream()` 是两个并发 Tasks 吗？为什么 `progress` 能在相邻结果之间产生
tick？

??? success "展开预测答案"

    `consume()` 驱动 `stream()`，两者处于**同一个 consumer Task 的异步调用链**，不是两个
    并发 Tasks。`progress` 才是另一个 Task。当 stream 等待下一批结果时，`await` 把控制权
    还给 event loop，progress 因而能够运行。

## 完整代码：`step_06_async_iteration.py`

```python
# Step 06：用 async for 一边消费流式结果，一边让另一个 progress Task 继续推进。
import asyncio
from collections.abc import AsyncIterator

from phi_async_lab.events import EventKind, EventLog
from phi_async_lab.scenario import SOURCES, SearchResult, SourceSpec, materialize

QUERY = "How do I cancel async work safely?"
DOCS_SOURCE = SOURCES[0]


async def stream(
    source: SourceSpec,
    query: str,
    event_log: EventLog,
) -> AsyncIterator[SearchResult]:
    event_log.record(source.name, EventKind.STARTED, "stream")
    results = materialize(source, query)
    delay_per_result = source.delay / len(results)

    for result in results:
        event_log.record(source.name, EventKind.WAITING, result.title)
        # 每次 yield 前都会 await，这是一个真正的挂起点：
        # progress Task 正是趁这个空隙才有机会运行。
        await asyncio.sleep(delay_per_result)
        event_log.record(source.name, EventKind.RESUMED, result.title)
        # yield 把当前结果交给调用者，同时记住这里的执行位置，
        # 下次被 __anext__ 驱动时从这里继续。
        yield result

    event_log.record(source.name, EventKind.COMPLETED, f"{len(results)} chunks")


async def consume(
    source: SourceSpec,
    query: str,
    event_log: EventLog,
    finished: asyncio.Event,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    try:
        # consume() 和 stream() 属于同一条调用链，不是两个并发 Task；
        # 真正独立的另一个 Task 是下面的 show_progress()。
        async for result in stream(source, query, event_log):
            results.append(result)
            event_log.record("consumer", EventKind.OBSERVED, result.title)
        return results
    finally:
        # 不管正常结束还是被打断，都要通知 progress 该停下来了。
        finished.set()


async def show_progress(finished: asyncio.Event, event_log: EventLog) -> None:
    event_log.record("progress", EventKind.STARTED)
    tick = 0
    while not finished.is_set():
        await asyncio.sleep(0.02)
        if not finished.is_set():
            tick += 1
            event_log.record("progress", EventKind.TICK, str(tick))
    event_log.record("progress", EventKind.COMPLETED)


async def run_streaming_demo(
    source: SourceSpec = DOCS_SOURCE,
    query: str = QUERY,
) -> tuple[list[SearchResult], EventLog]:
    event_log = EventLog()
    finished = asyncio.Event()

    async with asyncio.TaskGroup() as task_group:
        consumer_task = task_group.create_task(consume(source, query, event_log, finished))
        task_group.create_task(show_progress(finished, event_log))

    return consumer_task.result(), event_log


async def main() -> None:
    results, event_log = await run_streaming_demo()
    print("Step 06 - async iteration with progress")
    print("=======================================")
    print(event_log.render())
    print()
    print(f"results: {len(results)}")


if __name__ == "__main__":
    asyncio.run(main())
```

## 运行并观察

```bash
uv run python -m phi_async_lab.step_06_async_iteration
```

```text
01 | docs       | started    | stream
02 | docs       | waiting    | Coroutines and Tasks
03 | progress   | started
04 | progress   | tick       | 1
05 | docs       | resumed    | Coroutines and Tasks
06 | consumer   | observed   | Coroutines and Tasks
07 | docs       | waiting    | Task Cancellation
08 | progress   | tick       | 2
09 | progress   | tick       | 3
10 | docs       | resumed    | Task Cancellation
11 | consumer   | observed   | Task Cancellation
12 | docs       | waiting    | Asynchronous Iterators
...
17 | docs       | completed  | 3 chunks
18 | progress   | completed

results: 3
```

`async for` 的每次迭代都可能等待下一个值。`yield result` 把值交给 consumer，并保留
generator 的执行位置；下一次迭代再从该位置继续。

```text
consumer Task                         progress Task
     │                                     │
     ├─ 请求下一项                         ├─ tick
     │    stream: await I/O ── 挂起 ───────┤
     ├─ 收到 yield 的结果                  ├─ tick
     └─ 请求下一项                         └─ ...
```

`asyncio.Event` 在这里只是一个异步信号：consumer 无论正常结束还是被中断，都会在
`finally` 中设置 `finished`，让 progress Task 停止。它不是操作系统线程事件，也不创建
新的线程。

## 微实验：在 stream 中阻塞

临时把 `stream()` 内的：

```python
await asyncio.sleep(delay_per_result)
```

改为：

```python
import time

time.sleep(delay_per_result)
```

再次运行。三个结果仍会产生，但 progress 无法在等待期间 tick。这证明 `async for` 本身不
保证其他 Task 能运行；producer 内部仍必须使用 cooperative 的异步边界。

```bash
git restore src/phi_async_lab/step_06_async_iteration.py
```

## 用测试验证 streaming

```bash
uv run pytest tests/test_steps.py -k async_iterator
```

测试验证三个结果保持流式顺序，同时至少有一个 progress tick 出现在运行期间。它不要求
固定 tick 数量，因为调度速度不是业务契约。
