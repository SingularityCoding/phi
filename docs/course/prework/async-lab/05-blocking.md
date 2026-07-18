# Step 05：`async def` 也会阻塞 event loop

`async` 只允许函数在明确的异步边界挂起；它不会把函数体中的普通 Python 自动变成可抢占
代码。这一页同时运行“检索”和“心跳”，然后故意在检索中调用 `time.sleep()`。

## 先预测

`blocking_search()` 已经声明为 `async def`，并与 `heartbeat()` 一起放进 `TaskGroup`。
心跳会在搜索等待期间正常输出吗？

??? success "展开预测答案"

    不会。`time.sleep()` 是阻塞同步调用，没有把控制权交还 event loop。搜索 Task 开始后，
    event loop 无法推进 heartbeat，甚至不能让它记录第一个 `started` 事件，直到 sleep 返回。

## 完整代码：`step_05_blocking.py`

```python
import asyncio
import time
from collections.abc import Awaitable, Callable

from phi_async_lab.events import EventKind, EventLog

BLOCK_SECONDS = 0.12


async def blocking_search(event_log: EventLog) -> None:
    event_log.record("search", EventKind.STARTED, "blocking version")
    event_log.record("search", EventKind.WAITING, f"time.sleep({BLOCK_SECONDS:.2f})")
    time.sleep(BLOCK_SECONDS)
    event_log.record("search", EventKind.COMPLETED)


async def cooperative_search(event_log: EventLog) -> None:
    event_log.record("search", EventKind.STARTED, "cooperative version")
    event_log.record("search", EventKind.WAITING, f"asyncio.sleep({BLOCK_SECONDS:.2f})")
    await asyncio.sleep(BLOCK_SECONDS)
    event_log.record("search", EventKind.RESUMED)
    event_log.record("search", EventKind.COMPLETED)


async def heartbeat(event_log: EventLog) -> None:
    event_log.record("heartbeat", EventKind.STARTED)
    for number in range(1, 5):
        await asyncio.sleep(0.03)
        event_log.record("heartbeat", EventKind.TICK, str(number))
    event_log.record("heartbeat", EventKind.COMPLETED)


async def run_pair(operation: Callable[[EventLog], Awaitable[None]]) -> EventLog:
    event_log = EventLog()
    async with asyncio.TaskGroup() as task_group:
        task_group.create_task(operation(event_log))
        task_group.create_task(heartbeat(event_log))
    return event_log


async def main() -> None:
    blocking_log = await run_pair(blocking_search)
    cooperative_log = await run_pair(cooperative_search)

    print("Step 05A - a blocking call freezes the event loop")
    print("================================================")
    print(blocking_log.render())
    print()
    print("Step 05B - an await lets the heartbeat run")
    print("=============================================")
    print(cooperative_log.render())


if __name__ == "__main__":
    asyncio.run(main())
```

## 运行并对比

```bash
uv run python -m phi_async_lab.step_05_blocking
```

阻塞版本中，搜索完成后 heartbeat 才能开始：

```text
01 | search     | started    | blocking version
02 | search     | waiting    | time.sleep(0.12)
03 | search     | completed
04 | heartbeat  | started
05 | heartbeat  | tick       | 1
...
```

协作版本中，搜索在 `await asyncio.sleep()` 处允许 heartbeat 推进：

```text
01 | search     | started    | cooperative version
02 | search     | waiting    | asyncio.sleep(0.12)
03 | heartbeat  | started
04 | heartbeat  | tick       | 1
05 | heartbeat  | tick       | 2
06 | heartbeat  | tick       | 3
07 | search     | resumed
08 | search     | completed
09 | heartbeat  | tick       | 4
10 | heartbeat  | completed
```

这就是 cooperative 的含义：event loop 不会强制暂停 `blocking_search()`，只能等待它主动
到达一个能交还控制权的异步边界。

## 比喻的边界

可以把 event loop 想成管理“可继续任务卡”的调度员。`await asyncio.sleep()` 会把当前卡片
放入等待区，计时器到期后再放回 ready 队列；`time.sleep()` 则像工作人员拿着唯一的工作台
原地发呆，调度员拿不回工作台。

这个比喻不能推出 event loop 会监视任意代码：它看不到一个普通函数什么时候“适合暂停”，
也不会安全地从任意 Python 表达式中间抢走执行权。

## 微实验：破坏协作版本

把 `cooperative_search()` 中的：

```python
await asyncio.sleep(BLOCK_SECONDS)
```

临时改成：

```python
time.sleep(BLOCK_SECONDS)
```

再次运行，两个日志都会显示 heartbeat 被推迟。然后恢复：

```bash
git restore src/phi_async_lab/step_05_blocking.py
```

## 用测试验证“是否获得运行机会”

```bash
uv run pytest tests/test_steps.py -k heartbeat
```

测试不测量 heartbeat 晚了多少毫秒，只比较事件：阻塞版本中搜索先完成；协作版本中
heartbeat 在搜索完成前已经开始。

!!! info "选读：无法改写的同步函数"

    如果第三方同步 API 无法替换，可以用 `await asyncio.to_thread(sync_function, ...)` 把它
    移到工作线程。但这不是把同步函数变成 coroutine；取消等待它的 Task 也不代表底层线程
    已经停止。`to_thread()` 不属于 Readiness Check，主线优先使用原生异步 API。

[下一步：Async iterator 与 streaming →](06-async-iteration.md)
