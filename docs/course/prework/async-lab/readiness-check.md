# Async Readiness Check

不管你是刚做完 Async Lab 想复习一遍，还是觉得自己本来就熟悉异步编程、想直接跳过 Lab——
这份检查都是为你准备的。它是开放资料的自我校验，不计分、不用提交，也没有标准措辞，你只
需要对自己诚实。

题目本身和 Lab 里的检索场景无关，只考察抽象的异步编程概念，所以就算你没做过 Lab，也应该
能看懂每一道题在问什么。

## 怎么用这份检查

1. 展开任何折叠内容之前，先自己写下或说出答案；
2. 不要只猜输出结果，还要能解释背后的原因；
3. 卡住了？先展开**提示**——它只会给你一个思考方向，不会直接给结论；
4. 想清楚了，再展开**参考答案**，对照里面加粗的关键点；
5. 只要有一个关键点是遗漏、错误或说不清楚的，就回到题目链接的 checkpoint 重新过一遍；
6. 所有题目都能讲清楚，就说明你已经具备进入 Phi Chapter 01 所需的异步基础了。

## 题目 1：调用一个 `async def` 函数

下面的代码是否已经运行了 `do_work()` 的函数体？`value` 现在是什么？

```python
async def do_work() -> str:
    ...

value = do_work()
```

??? question "提示"

    调用一个 `async def` 函数，和调用一个普通函数，两者的返回值是同一种东西吗？

??? success "展开参考答案"

    函数体**尚未开始运行**。调用 `async def` 函数得到的是一个 **coroutine object**，不是
    最终结果，也不是已经被调度的 Task。coroutine 必须被 `await`，或者包装成 Task 后由
    event loop 驱动，函数体才会真正开始执行。

    如果创建后就丢在一边，通常还会看到 "coroutine was never awaited" 的警告。

    复习：[Step 02：Coroutine](02-coroutines.md)

## 题目 2：`await` 是否必然切换 Task

有人说：“只要执行到 `await`，event loop 就一定会先去运行另一个 Task。”这句话对吗？

??? question "提示"

    如果 `await` 后面的东西其实已经准备好了，根本不需要真正等待，会发生什么？

??? success "展开参考答案"

    不对。`await` **允许当前 Task 挂起**，但只有当 awaitable 还没完成、当前 Task 确实需要
    等待时，控制权才会交还给 event loop。一个已经完成的 awaitable 可以立刻返回，所以
    `await` **不保证每次都发生任务切换**。

    event loop 也不会在普通 Python 代码的任意一行强行抢占执行。

    复习：[Step 02：`await` 的运行模型](02-coroutines.md#await)

## 题目 3：顺序 `await` 与 Tasks

比较下面两段代码。哪一段会让多个独立请求真正并发推进？为什么？

```python
# A
results = []
for item in items:
    results.append(await fetch(item))

# B
tasks = [asyncio.create_task(fetch(item)) for item in items]
results = [await task for task in tasks]
```

??? question "提示"

    留意一下：“创建 Task”和“开始等待某个 Task 的结果”，在两段代码里分别发生在什么时候？

??? success "展开参考答案"

    A 是**顺序等待**：当前 `fetch()` 完整返回之后，下一个 coroutine 才会被调用。

    B 先创建了**全部 Tasks**，然后才开始等待结果。等待第一个 Task 时，其他已经登记到
    event loop 的 Tasks 也能继续推进。因此 B 形成了 I/O concurrency，即使最后读取结果时
    仍然按列表顺序。

    `create_task()` 本身不会抢占当前代码；当前 Task 仍需要到达 `await` 等交还控制权的
    位置，其他 Task 才有机会运行。

    复习：[Step 03：Tasks](03-tasks.md)

## 题目 4：`TaskGroup` 与 `async with`

下面的代码运行到 `print(task.result())` 时，`child()` 是否可能仍在运行？这里的
`async with` 表达了什么？

```python
async with asyncio.TaskGroup() as task_group:
    task = task_group.create_task(child())

print(task.result())
```

??? question "提示"

    正常离开一个作用域（没有异常、没有取消）之前，`TaskGroup` 必须先确认一件事——是
    什么？

??? success "展开参考答案"

    正常离开 `TaskGroup` 作用域后，`child()` **不可能仍在运行**。退出过程会等待组中所有
    Tasks 完成；失败或取消路径也负责处理仍未结束的 sibling tasks。

    `async with` 表示进入或退出资源作用域本身可以包含异步等待。这里它表达的是**子任务的
    所有权和有界生命周期**，而不只是一种缩进风格。

    复习：[Step 04：结构化并发](04-structured-concurrency.md)

## 题目 5：为什么另一个 Task 会被冻结

`heartbeat()` 与下面的函数已经作为两个 Tasks 创建。为什么 `heartbeat` 仍可能完全停止
0.5 秒？最小的修正是什么？

```python
async def load() -> None:
    time.sleep(0.5)
```

??? question "提示"

    `async def` 这几个字，改变的是“函数在哪些位置可以挂起”，还是“函数体里每一行代码
    都能随时被打断”？

??? success "展开参考答案"

    `async def` 不会自动让函数体变得可抢占。`time.sleep()` 是**阻塞同步调用**，执行期间
    不会把控制权交还 event loop，所以同一个 loop 上的 `heartbeat` 无法推进。

    最小修正是换成原生异步等待：

    ```python
    await asyncio.sleep(0.5)
    ```

    真实项目应优先使用目标库自带的异步 API；`to_thread()` 只是在实在无法替换同步 API 时
    的桥接方式。

    复习：[Step 05：阻塞 event loop](05-blocking.md)

## 题目 6：Async iterator 与并发

下面的 `consume()` 与 `stream()` 是否自动成为两个 Tasks？另一个独立的 progress Task，最
有机会在什么位置运行？

```python
async def consume() -> None:
    async for chunk in stream():
        render(chunk)
```

??? question "提示"

    consumer 请求“下一项”时，如果这一项还没准备好，是谁来决定要不要把控制权交还
    event loop？

??? success "展开参考答案"

    不会。consumer Task 通过 `async for` 驱动 async iterator，`consume()` 与 `stream()`
    属于**同一条异步调用链**。`async for` 在请求下一项时可能等待；当 `stream()` 内部等待
    尚未就绪的 I/O、把控制权交还 event loop 时，独立的 progress Task 才有机会运行。

    `yield` 保存 generator 的恢复位置，并把一个值交给 consumer，但 async iterator 本身不
    保证其他 Tasks 一定会获得运行机会；producer 内部仍然不能阻塞 loop。

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

??? question "提示"

    `except CancelledError` 里返回一个普通值，和清理完之后重新 `raise`，调用者能感知到的
    “任务终止状态”是一样的吗？

??? success "展开参考答案"

    代码**吞掉了 `CancelledError`**，把取消伪装成了一个普通字符串结果。调用者
    `await task` 会得到 `"cancelled"`，完全无法从 Task 的终止状态知道工作其实是被取消的。

    正确的写法通常在 `finally` 中完成 cleanup，或者在 `except CancelledError` 里清理后
    **重新 `raise`**。`task.cancel()` 只是发出请求；调用者还必须 `await task`，让 coroutine
    有机会观察取消、执行 cleanup，并把真实的终止状态传播出去。

    在 `asyncio.timeout()` 中，deadline 同样是通过 cancellation 中断内部等待；离开
    timeout 作用域后，调用者观察到的是 `TimeoutError`。

    复习：[Step 07：Cancellation](07-cancellation.md)

## 题目 8：谁拥有 event loop

下面的 `run_query()` 被 Textual 的 callback 或者异步 pytest 调用时会失败。为什么？应该
怎么改？

```python
async def run_query() -> list[str]:
    return asyncio.run(collect())
```

??? question "提示"

    Textual 和异步 pytest 在调用 `run_query()` 之前，是不是已经有一个 event loop 在
    运转了？

??? success "展开参考答案"

    Textual 和异步 pytest 都已经拥有一个正在运行的 event loop；在其中再次调用
    `asyncio.run()` 会尝试创建一个嵌套 loop，抛出 “cannot be called from a running event
    loop”。

    异步调用链里应该直接 **`return await collect()`**。只有最外层、还没有 event loop 的
    同步程序入口，才需要调用一次 `asyncio.run(main())`。event loop 的所有权属于 Host 或
    运行入口，不是任意一个业务函数。

    复习：[Step 02：谁调用 `asyncio.run()`](02-coroutines.md#asynciorun)

## 通过之后

如果这 8 道题你都能在不先展开提示或答案的情况下讲清楚，就已经具备进入 Phi 正课所需的
异步编程基础了。下一步可以按照 [环境准备](../../appendix/setup.md) 初始化 Starter
Repository，然后进入 [Chapter 01：Model Boundary](../../chapters/01-model-boundary.md)。

如果某道题还是靠猜，先别急着记答案——回到题目链接的 checkpoint，重新预测一遍事件顺序、
跑一次代码、做一次破坏实验，再回来试着自己解释原因。
