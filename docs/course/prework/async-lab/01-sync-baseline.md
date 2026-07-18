# Step 01：从同步基线开始

异步编程真正要解决的问题不是「让函数名看起来更现代」，而是：**当一段代码只能干等外部
结果的时候，同一个线程能不能先去干点别的？** 在改动任何代码之前，我们先看看你已经很
熟悉的同步版本长什么样。

## 先预测

三个数据源分别等待 0.12、0.08 和 0.04 秒。下面的循环会以什么顺序开始和完成？总耗时
更接近 0.12 秒，还是 0.24 秒？

??? success "展开预测答案"

    `docs` 会先完成，随后才开始 `issues`，最后才开始 `notes`。等待时间不能重叠，所以总耗时
    接近 **0.12 + 0.08 + 0.04 = 0.24 秒**。

## 完整代码：`step_01_sync.py`

```python
# Step 01：同步基线。没有 async，也没有并发——三个数据源严格排队执行。
import time
from collections.abc import Sequence

from phi_async_lab.events import EventKind, EventLog
from phi_async_lab.reporting import print_report
from phi_async_lab.scenario import SOURCES, SearchResult, SourceSpec, materialize

QUERY = "How do I cancel async work safely?"


def search(source: SourceSpec, query: str, event_log: EventLog) -> list[SearchResult]:
    event_log.record(source.name, EventKind.STARTED, query)
    event_log.record(source.name, EventKind.WAITING, f"{source.delay:.2f}s")
    # time.sleep() 是阻塞调用：线程原地等待，没有把控制权让给任何人。
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
    # 普通 for 循环 + 普通函数调用：下一个 search() 必须等上一个完整返回才会开始。
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

`time.sleep()` 就是一次实打实的阻塞等待。线程在这段时间里没法推进 `collect()`，所以第
二个数据源连 `started` 事件都记录不上。CPU 其实没在忙着计算什么，但这段等待时间也完全
没被利用起来——这就是同步代码最大的浪费。

## 建立你的第一个心智模型

可以把当前线程想象成只有一个工作人员：他把请求交给 `docs` 后，就站在原地干等结果，
等到了才肯去处理下一张任务卡。这个比喻只描述同步等待；后面你会看到，event loop 并不会
真的多请几个工作人员，而是让同一个工作人员在某张任务卡还在等待时，先去处理另一张已经
能往前推进的任务卡。

## 微实验：故意调慢一个数据源

把 `scenario.py` 里 `docs.delay` 从 `0.12` 改成 `0.50`，再运行一次。先猜猜看：只有
`docs` 变慢了，`issues` 和 `notes` 的开始时间会不会也跟着被拖后？

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
