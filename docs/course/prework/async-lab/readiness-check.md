# Async Readiness Check

这份检查既是 Async Lab 的最终验收，也是已有异步编程经验学生的 test-out 入口。它是开放
资料的自我校验，不计分、不提交，也不要求固定措辞。

## 如何使用

1. 在展开答案前，先写下或完整说出自己的答案；
2. 不只预测输出，还要解释因果；
3. 展开参考答案，对照其中的**加粗关键点**；
4. 任一关键点遗漏、错误或不确定，就回到题目链接的 checkpoint；
5. 所有核心题都能完整解释，才算具备进入 Phi Chapter 01 所需的异步基础。

## 题目 1：调用 coroutine

下面的代码是否已经运行了 `search()` 函数体？`value` 是什么？

```python
value = search(SOURCES[0], QUERY, EventLog())
```

??? success "展开参考答案"

    函数体**尚未开始运行**。调用 `async def` 函数得到的是一个 **coroutine object**，不是
    最终结果，也不是已经被调度的 Task。coroutine 必须被 `await`，或包装成 Task 后由 event
    loop 驱动。

    如果只创建后丢弃，通常还会得到 “coroutine was never awaited” 警告。

    复习：[Step 02：Coroutine](02-coroutines.md)

## 题目 2：`await` 是否必然切换 Task

有人说：“只要执行到 `await`，event loop 就一定会先运行另一个 Task。”这句话正确吗？

??? success "展开参考答案"

    不正确。`await` **允许当前 Task 挂起**；只有 awaitable 尚未完成、当前 Task 需要等待时，
    控制权才会交还 event loop。一个已经完成的 awaitable 可以立即返回，所以 `await` **不保证
    每次发生任务切换**。

    event loop 同样不会在普通 Python 代码的任意一行抢占执行。

    复习：[Step 02：`await` 的运行模型](02-coroutines.md#await)

## 题目 3：顺序 `await` 与 Tasks

比较下面两段代码。哪一段会让三个独立请求并发推进？为什么？

```python
# A
results = []
for source in sources:
    results.extend(await search(source))

# B
tasks = [asyncio.create_task(search(source)) for source in sources]
results = [await task for task in tasks]
```

??? success "展开参考答案"

    A 是**顺序等待**：当前 `search()` 完整返回后，下一个 coroutine 才被调用。

    B 先创建全部 **Tasks**，然后才等待结果。等待第一个 Task 时，其他已经登记到 event loop
    的 Tasks 也能推进。因此 B 形成 I/O concurrency，即使最后按列表顺序读取结果。

    `create_task()` 本身不会抢占当前代码；当前 Task 仍需到达 `await` 等交还控制权的位置。

    复习：[Step 03：Tasks](03-tasks.md)

## 题目 4：`TaskGroup` 与 `async with`

下面的代码运行到 `print(task.result())` 时，`child()` 是否可能仍在运行？`async with` 在这里
表达了什么？

```python
async with asyncio.TaskGroup() as task_group:
    task = task_group.create_task(child())

print(task.result())
```

??? success "展开参考答案"

    正常离开 `TaskGroup` 作用域后，`child()` **不可能仍在运行**。退出过程会等待组中的所有
    Tasks 完成；失败或取消路径也负责处理仍未结束的 sibling tasks。

    `async with` 表示进入或退出资源作用域本身可以包含异步等待。这里它表达的是**子任务的
    所有权和有界生命周期**，而不只是缩进风格。

    复习：[Step 04：结构化并发](04-structured-concurrency.md)

## 题目 5：为什么 heartbeat 冻结

`heartbeat()` 与下面的函数已经作为两个 Tasks 创建。为什么 heartbeat 仍可能完全停止
0.5 秒？最小修正是什么？

```python
async def load() -> None:
    time.sleep(0.5)
```

??? success "展开参考答案"

    `async def` 不会自动让函数体可抢占。`time.sleep()` 是**阻塞同步调用**，执行期间不会把
    控制权交还 event loop，所以同一个 loop 上的 heartbeat 无法推进。

    在这个模拟场景中，最小修正是使用原生异步等待：

    ```python
    await asyncio.sleep(0.5)
    ```

    真实项目应优先使用目标库的异步 API；`to_thread()` 只是无法替换同步 API 时的桥接方式。

    复习：[Step 05：阻塞 event loop](05-blocking.md)

## 题目 6：Async iterator 与并发

下面的 `consume()` 与 `stream()` 是否自动成为两个 Tasks？在什么位置，另一个 progress
Task 最有机会运行？

```python
async def consume() -> None:
    async for chunk in stream():
        render(chunk)
```

??? success "展开参考答案"

    不会。consumer Task 通过 `async for` 驱动 async iterator，`consume()` 与 `stream()`
    属于**同一条异步调用链**。`async for` 在请求下一项时可能等待；当 `stream()` 内部等待
    尚未就绪的 I/O 并把控制权交还 event loop 时，独立的 progress Task 才能运行。

    `yield` 保存 generator 的恢复位置并把一个值交给 consumer，但 async iterator 本身不保证
    其他 Tasks 会获得运行机会；producer 内部仍不能阻塞 loop。

    复习：[Step 06：Async iterator](06-async-iteration.md)

## 题目 7：取消与 cleanup

下面的处理有什么问题？调用者执行 `task.cancel(); await task` 后会观察到什么？

```python
async def operation() -> str:
    try:
        await wait_for_data()
        return "done"
    except asyncio.CancelledError:
        await cleanup()
        return "cancelled"
```

??? success "展开参考答案"

    代码**吞掉了 `CancelledError`**，并把取消伪装成普通字符串结果。调用者 `await task` 会
    得到 `"cancelled"`，无法从 Task 的终止状态知道工作被取消。

    正确控制流通常在 `finally` 中完成 cleanup，或在 `except CancelledError` 中清理后
    **重新 `raise`**。`task.cancel()` 只是请求；调用者还必须 `await task`，让 coroutine 有机会
    观察取消、执行 cleanup，并传播真实终止状态。

    在 `asyncio.timeout()` 中，deadline 同样通过 cancellation 中断内部等待；离开 timeout
    作用域后，调用者观察到 `TimeoutError`。

    复习：[Step 07：Cancellation](07-cancellation.md)

## 题目 8：谁拥有 event loop

下面的 `run_query()` 由 Textual callback 或异步 pytest 调用。为什么会失败？应该如何改？

```python
async def run_query() -> list[str]:
    return asyncio.run(collect())
```

??? success "展开参考答案"

    Textual 和异步 pytest 已经拥有正在运行的 event loop；在其中再次调用 `asyncio.run()` 会
    尝试创建嵌套 loop，并报 “cannot be called from a running event loop”。

    异步调用链应直接 **`return await collect()`**。只有最外层、尚无 event loop 的同步程序
    入口通常调用一次 `asyncio.run(main())`。event-loop ownership 属于 Host 或运行入口，
    不是任意业务函数。

    复习：[Step 02：谁调用 `asyncio.run()`](02-coroutines.md#asynciorun)

## 通过之后

如果你能在不先展开答案的情况下完整解释所有加粗关键点，就已经具备进入 Phi 正课所需的
异步编程基础。下一步按照 [环境准备](../../appendix/setup.md) 初始化 Starter Repository，
然后进入 [Chapter 01：Model Boundary](../../chapters/01-model-boundary.md)。

如果某一题仍依赖猜测，不要死记答案；回到链接的 checkpoint，重新预测事件顺序、运行代码、
做一次破坏实验，再回来解释因果。
