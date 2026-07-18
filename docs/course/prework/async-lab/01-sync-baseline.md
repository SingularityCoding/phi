# Step 01：从同步基线开始

异步编程要解决的不是“如何让函数名看起来更现代”，而是：**当一个任务只能等待外部结果
时，同一个线程能否先推进别的工作？** 在改变代码前，先观察熟悉的同步版本。

## 先预测

三个数据源分别等待 0.12、0.08 和 0.04 秒。下面的循环会以什么顺序开始和完成？总耗时
更接近 0.12 秒，还是 0.24 秒？

??? success "展开预测答案"

    `docs` 会先完成，随后才开始 `issues`，最后才开始 `notes`。等待时间不能重叠，所以总耗时
    接近 **0.12 + 0.08 + 0.04 = 0.24 秒**。

## 完整代码：`step_01_sync.py`

```python
import time
from collections.abc import Sequence

from phi_async_lab.events import EventKind, EventLog
from phi_async_lab.reporting import print_report
from phi_async_lab.scenario import SOURCES, SearchResult, SourceSpec, materialize

QUERY = "How do I cancel async work safely?"


def search(source: SourceSpec, query: str, event_log: EventLog) -> list[SearchResult]:
    event_log.record(source.name, EventKind.STARTED, query)
    event_log.record(source.name, EventKind.WAITING, f"{source.delay:.2f}s")
    time.sleep(source.delay)
    event_log.record(source.name, EventKind.RESUMED)
    results = materialize(source, query)
    event_log.record(source.name, EventKind.COMPLETED, f"{len(results)} results")
    return results


def collect(
    query: str,
    sources: Sequence[SourceSpec] = SOURCES,
    event_log: EventLog | None = None,
) -> tuple[list[SearchResult], EventLog]:
    log = event_log or EventLog()
    results: list[SearchResult] = []
    for source in sources:
        results.extend(search(source, query, log))
    return results, log


def main() -> None:
    started_at = time.perf_counter()
    results, event_log = collect(QUERY)
    elapsed = time.perf_counter() - started_at
    print_report("Step 01 - synchronous baseline", event_log, results, elapsed)


if __name__ == "__main__":
    main()
```

## 运行并观察

```bash
uv run python -m phi_async_lab.step_01_sync
```

输出中的小数可能略有差异，但事件顺序稳定：

```text
01 | docs       | started    | How do I cancel async work safely?
02 | docs       | waiting    | 0.12s
03 | docs       | resumed
04 | docs       | completed  | 3 results
05 | issues     | started    | How do I cancel async work safely?
06 | issues     | waiting    | 0.08s
07 | issues     | resumed
08 | issues     | completed  | 2 results
09 | notes      | started    | How do I cancel async work safely?
10 | notes      | waiting    | 0.04s
11 | notes      | resumed
12 | notes      | completed  | 2 results

results: 7
elapsed: 0.24s
```

`time.sleep()` 代表一次阻塞等待。线程在这段时间不能推进 `collect()`，所以第二个数据源连
`started` 事件都无法记录。CPU 并没有忙于计算，但程序也没有利用这段等待时间。

## 建立第一个模型

可以把当前线程想成只有一个工作人员：他把请求交给 `docs` 后，站在原地等到结果返回，
才去处理下一张任务卡。这个比喻只描述同步等待；后面 event loop 不会真的增加工作人员，
而是让同一个线程在某张任务卡等待时切换到另一张已经可以推进的任务卡。

## 微实验：改变最慢数据源

把 `scenario.py` 中 `docs.delay` 从 `0.12` 改成 `0.50`，再次运行。先预测：只有 `docs`
变慢，`issues` 和 `notes` 的开始时间会不会也被推迟？

??? success "展开解释"

    会。同步调用形成严格的调用栈，`search(docs)` **返回之前**，循环不会进入
    `search(issues)`。最慢数据源不只推迟自己的结果，也推迟所有排在后面的请求。

完成后恢复文件：

```bash
git restore src/phi_async_lab/scenario.py
```

## 用测试观察同一个事实

```bash
uv run pytest tests/test_steps.py -k sync_collection
```

测试不比较 0.24 秒这个墙钟数字，而是断言一个更稳定的事实：当前数据源的 `completed`
一定发生在下一个数据源的 `started` 之前。

```python
for current, following in zip(FAST_SOURCES, FAST_SOURCES[1:], strict=False):
    assert event_index(event_log.events, current.name, EventKind.COMPLETED) < event_index(
        event_log.events, following.name, EventKind.STARTED
    )
```

[下一步：Coroutine 与顺序 await →](02-coroutines.md)
