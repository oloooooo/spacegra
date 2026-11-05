# 子任务只做取消与状态通知，不做 eval 前的 HITL 审批

> **已归档**：本文是历史记录，不再约束当前代码；现行决定见 [`../README.md`](../README.md) 总表。
> 归档于 2026-11-02：反决定已固化在 `custom/api/tasks.py` 与 `shared/outbox.py` 的注释里。

「暂停 / 取消 / 继续 / 崩溃要让人知道」这条需求，最初的设计是给子 agent 的 `eval`
加一道人工审批闸门（`interrupt_on` + 动态 `when` + `auto_approve` 记住放行 + 审批卡 +
分发者冻结）。**这条链路整个不做了**，原因只有一个：`eval` 没有需要保护的东西。

## 为什么 `eval` 不需要审批

| 能力 | 实际配置 | 结论 |
|---|---|---|
| `eval` 沙箱 | QuickJS（`quickjs_rs`），导出符号只有 `Runtime`/`Context`/`JSError` 等引擎控制，`langchain_quickjs/_repl.py` 里没有 `open`/`socket`/`requests` | 做不了文件与网络 IO |
| `eval` 能调的 `ptc` 工具 | `XLSX_TOOLS = xlsx_info / xlsx_query / xlsx_search / xlsx_profile`（`graphs/tools/xlsx/__init__.py:287`） | 四个全是只读 |
| 文件写 | `FilesystemPermission(write, "/", deny)` | 写不了 |
| backend | 空间隔离只读 | 写不了共享空间 |

于是这道闸门拦不住任何不可撤销的动作，只会让用户每跑一次代码点一次「可以」。
按 LangChain HITL 的口径，审批只该用在「退款 / 删数据 / 发邮件 / 改生产配置」这类
**有真实影响**的动作上——这里一个都不沾。

**未来若要放开写**（`write_file` 或新增 `xlsx_write`），审批就是必需的，并且要连带解决
「两个子 agent 同时写同一个 Excel」「写前备份」「审计轨迹」——那是独立需求，
不该混进「暂停 / 继续」。届时另起 ADR。

## 那么需求怎么落地

| 子需求 | 做法 |
|---|---|
| **取消** | 主管用 deepagents 内置的 `cancel_async_task`（无需改动）；用户在侧栏任务卡点「停止」→ `POST /custom/tasks/{tid}/cancel`，后端以服务身份代理一次 `runs.cancel`（子线程属主是服务账号，用户 token 打不通） |
| **崩溃通知** | `lease_reaper` 重试用尽时补写发件箱行（`status='error'`）。此前它只把 `runs.status` 置 `error`，**主管永远等一个再也不会来的结果** |
| **状态通知** | 子 run 落 `cancelled` 时，主管下一轮 `abefore_agent` 发一条 `[Async subagent 'x' was cancelled]`（不带 `Result:`），前端渲染成灰色状态条 |
| **暂停 / 继续 / 审批** | 不做 |

## 三个关键决定

### 1. `interrupted` 在父 state 里落成两个词

服务端只有 `interrupted` 一个状态，靠 `interrupted_by` 区分两件事：

```
interrupted_by='cancel' → 父 state 写 cancelled    这一轮作废，没有结果
interrupted_by='hitl'   → 父 state 写 interrupted  现场还在（本期不产生）
```

不映射的话会出真 bug：`cancel_async_task` 工具把 state 写成 `cancelled`，而下一次
`_refreshed_*` 把它刷成服务端的 `interrupted`，同一个状态两个词；之后判重和通知
都会按不同的键算，重复或多发。

### 2. 结果和状态走两条路，各用各的判重键

| | 结果（`success`/`error`/`timeout`） | 状态（`cancelled`/`interrupted`） |
|---|---|---|
| 通道 | 发件箱行 + claim 互斥 | 直接写 state，无 channel |
| 判重键 | `delivered_run_id` | `notified_run_id` |
| 正文 | `[Async subagent 'x' has completed] Result: …` | `[Async subagent 'x' was cancelled]` |
| 为什么 | 结果要跨进程去重（分发者也会投） | 状态只告知"这轮没跑完"，不存在竞争 |

`_amark_checked`（模型自己 check 到终态）只对**结果**记账：check 到 `cancelled` 不该
算「已入上下文」，因为没有内容可看。

### 3. 取消之后不主动唤醒主管

子 run 被取消时 `finalize_run` 不入账（`interrupted` 不写发件箱行），所以主管是在
**下一轮 run 的 `abefore_agent`** 里通过 `_refreshed_*` 看到 `cancelled`，然后发状态通知。

代价：用户取消后如果不再和主管说话，主管就一直不知道。这是有意接受的——为一个
「任务被取消了」的提示单独唤醒一轮 LLM 不划算，而且用户自己知道刚点了什么。
崩溃不同：结果是**确定没了**，主管必须知道，所以崩溃走发件箱（见上面的表）。

## Consequences

- **正面**：省掉 5 处改动（`state_schema` / `interrupt_on` / `approval_api` / 审批卡 /
  分发者冻结）与一个常态打扰；`eval` 无副作用这件事本身也让「崩溃重试 = 整个 run 重跑」
  变得安全（`_resolve_input` 传的是原始 input，重试不是续跑）。
- **负面**：将来放开写时要补审批，且那时要重新设计（见「为什么不需要审批」的末段）。
- **负面**：用户取消后主管不会立刻知道（见决定 3）。
- **风险**：`lease_reaper` 的重试用尽通知依赖子线程 metadata 里的 `parent_thread_id`。
  手工建的线程没有这个字段，不会入账——与 ADR-0010 的认定方式一致，不是新问题。

## Considered Options

- **给 `eval` 上 HITL（原方案）**：技术选型是对的（deepagents `interrupt_on` 在
  `after_model` 拦截，工具还没执行，恢复不会重放副作用；动态 `when` 也避开了
  `interrupt_before` 编译期固定的限制），但**用错了对象**——为一个只读沙箱建审批卡。
- **先放开写、再上审批**：是独立需求，要连带解决并发写、备份、审计。不塞进本期。
- **不做取消入口，只靠主管调 `cancel_async_task`**：用户想停就得先说话让主管动手，
  多一轮 LLM 且主管未必听话。用户自己该能停。

## 2026-10-25 复审：只做取消，「继续」仍然不做

需求方又提了一次「暂停 + 取消 + 继续」。复审后维持：**只做取消**。

- **LangGraph 不能原地暂停**：能做的只是「停 run（checkpoint 留 `next`）→ 以后开一个
  `input=None` 的新 run，未完成的那一步重跑」。省下的是子 agent 已完成步的上下文重建，
  不是全部重来——价值比「继续」这个词听起来小。
- **真正的代价在父 state**：新 run 的 `run_id` 必须写回 `async_tasks[tid]`，否则投递/
  判重、`check_async_task` 全按旧 run_id 算，结果永远投不出来。今天没有任何代码从外部写
  父 state，而写它还要确认不与 HITL `interrupted` 线程打架。再加一个 `paused` 状态词、
  guard 的终态/通知/判重改造、双击幂等——三个文件起步，换一个很少按的按钮。
- **跑偏场景下续跑是错的**：用户停下来的典型理由是「它理解歪了」，这时该丢掉被污染的
  上下文重派（`start_async_task`），而不是接着跑。取消通知里已经写了「别自己重派，先问用户」。

本轮实际改动（无新状态、无新端点）：任务卡「停止」→「取消」；子任务状态映射收进
`custom/outbox.task_status_of_run`，前端代理不再把 `interrupted` 冒充成取消；取消后前端
立刻拉一次真实状态，避免父 state 里陈旧的 `running` 把卡片刷回去。
