# Step 04：用 `TaskGroup` 表达结构化并发

并发不只关心“如何同时开始”，还要回答：**这些子任务属于谁，作用域结束时谁保证它们都
已经完成或被清理？** Python 3.12 的 `asyncio.TaskGroup` 把答案写进代码结构。

## `async with` 的作用

普通 `with` 管理同步进入和退出；`async with` 允许进入或退出过程本身需要等待。对
`TaskGroup` 来说：

- 进入作用域时建立一组有共同所有者的子任务；
- `create_task()` 把子任务登记到组中；
- 离开作用域前等待组中所有 Tasks；
- 若作用域或子任务失败，组负责取消并等待尚未结束的 sibling tasks。

本 Lab 要求会使用和推理 `async with`，不要求实现 `__aenter__()` 或 `__aexit__()`。

## 先预测

`task.result()` 为什么放在 `async with` 之后？如果某个数据源需要 0.12 秒，程序能否在
它完成前离开 `TaskGroup`？

??? success "展开预测答案"

    不能。正常离开 `TaskGroup` 作用域意味着其中的 Tasks 已经全部完成，所以作用域外读取
    `task.result()` 是安全的。若仍有未完成子任务，退出过程会先等待。

## 完整代码：`step_04_task_group.py`

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
    tasks: list[asyncio.Task[list[SearchResult]]] = []

    async with asyncio.TaskGroup() as task_group:
        for source in sources:
            task = task_group.create_task(
                search(source, query, log),
                name=f"search:{source.name}",
            )
            tasks.append(task)

    results = [result for task in tasks for result in task.result()]
    return results, log


async def main() -> None:
    started_at = time.perf_counter()
    results, event_log = await collect(QUERY)
    elapsed = time.perf_counter() - started_at
    print_report("Step 04 - structured concurrency", event_log, results, elapsed)


if __name__ == "__main__":
    asyncio.run(main())
```

## 运行并观察

```bash
uv run python -m phi_async_lab.step_04_task_group
```

成功路径的事件顺序与 Step 03 相同：三个请求先开始，`notes` 最先完成，`docs` 最后完成，
总耗时接近 0.12 秒。差异不在成功输出，而在代码表达的生命周期保证。

```text
async with TaskGroup()       # 所有权作用域开始
    ├─ Task(docs)
    ├─ Task(issues)
    └─ Task(notes)
                              # 退出前等待整个集合
task.result()                 # 此处所有 Task 已终止
```

结果仍按照 `tasks` 列表顺序组合，因此**结果顺序**不必等于**完成顺序**。

## 为什么主线不使用 `gather()`

你会在很多 Python 代码中看到 `asyncio.gather()`。它也能并发等待多个 awaitables，但这个
Lab 先建立更重要的结构：子任务应存在于明确的生命周期作用域内。理解 Task 与 TaskGroup 后，
以后阅读 `gather()` 会更容易；反过来只记住“把 coroutine 列表塞进 gather”容易忽略任务
所有权、失败和清理。

## 微实验：观察作用域边界

在 `async with` 中、创建三个 Tasks 之后插入：

```python
print([task.done() for task in tasks])
```

在作用域外、构建 `results` 之前再插入同一行。第一次通常看到尚未完成的 Tasks；第二次
一定全部是 `True`。不要依赖第一次的具体布尔组合——真正的保证只存在于作用域退出之后。

完成后恢复文件：

```bash
git restore src/phi_async_lab/step_04_task_group.py
```

## 用测试验证结构

```bash
uv run pytest tests/test_steps.py -k task_group
```

测试同时验证所有请求在第一个结果前开始，以及最终结果保持输入数据源顺序。下一页会故意
破坏 cooperative scheduling，证明 `async def` 内部仍然可以冻结整个 loop。

!!! info "选读：子任务异常"

    如果一个 `TaskGroup` 子任务抛出未处理异常，组会取消其他未完成子任务，并在退出时通过
    `ExceptionGroup` 报告失败。`except*` 和多异常聚合不属于本 Lab 的 Readiness Check；
    Phi 正式实现需要时会在具体失败策略中讨论。

[下一步：阻塞 event loop →](05-blocking.md)
