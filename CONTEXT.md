# aegra-api — async subagent 的 ACP 服务面

aegra-api 是自托管的 Agent Protocol 服务；本 context 覆盖它作为 Deep Agents async subagent 通信面（即 Agent Protocol，简称 ACP）时承载的 Run/Thread 生命周期与状态词表。需求与设计见 [`docs/design.md`](./docs/design.md)。

## Language

**Agent Protocol (ACP)**:
外部权威协议，规范见 langchain-ai/agent-protocol；本仓库也简称 ACP。语义以规范为准，传输形状以真实客户端（langgraph_sdk / deepagents）实发为准；冲突处按 ADR 记录。
_Avoid_: Agent API、Aegra 协议

**Agent Server**:
该协议的官方托管实现（LangSmith Deployment），是"生产怎么跑"的参照物。
_Avoid_: 平台、LangGraph Platform

**Run**:
一次 agent 调用。要么属于某个 Thread（会更新其状态），要么是 stateless run。
_Avoid_: 任务、job、invocation

**Thread**:
一段对话状态（values/messages）的载体，可被多次 Run 更新。async subagent 的记忆就在子 agent 自己的 Thread 上，与 supervisor 的记忆互不相通。
_Avoid_: session、conversation、记忆

**Run status**:
Run 的唯一状态字段。终态只有 success / error / timeout / interrupted；活动态为 pending（排队中）与 running（已开始执行）。服务端没有 cancelled。
_Avoid_: 状态机阶段、phase

**Cancel action**:
取消动作的取值，取规范的 interrupt（仅取消）与 rollback（取消后删除该 Run 及其 checkpoint）；本服务额外接受 cancel 作为 interrupt 的别名，且 rollback 未实现。
_Avoid_: 把 Cancel action 当成 Run status

**Cancel（取消）**:
客户端请求某个 Run 立刻停止。Run 落终态 interrupted，Thread 归 idle，**不可续跑**。
父 state 里记作 `cancelled`（服务端只有 interrupted，靠 `interrupted_by` 区分，见 ADR-0014）。
_Avoid_: 撤销、中止；不要把它当成"暂停"

**HITL interrupt（人工中断）**:
图自己在需要人工审批处暂停（`interrupt_on` + checkpointer）。Run 落终态 interrupted，Thread 落 interrupted，**可续跑**。
_Avoid_: 把 HITL interrupt 说成 interrupt（与 Cancel action 同词不同义）

**Supersede（并发覆盖）**:
同一 Thread 上已有活动 Run 时，新 Run 顶替旧 Run（`multitask_strategy`）。客户端用 `multitask_strategy: "interrupt"` 表达"杀掉旧 Run、**保留 Thread 历史**、新建 Run"。
_Avoid_: 把 Supersede 叫 interrupt；把它与 Cancel 混为一谈

**Async subagent**:
Deep Agents supervisor 通过 Agent Protocol 把另一个 agent 当作后台任务调用。任务句柄是 thread_id，进度靠轮询 Run 状态，结果取自 Thread 的 values.messages。
_Avoid_: 后台任务、worker

**Supervisor**:
发起并轮询 async subagent 的 Deep Agents 主机。它只看到任务的本地状态；`cancelled` 是 supervisor 自己记录的 task 状态，不是服务端的 Run status。
_Avoid_: 主 agent、调度器

**Default assistant（默认 assistant）**:
`aegra.json` 里每个 graph 自动拥有、供 `assistant_id=graph_id` 直连的 assistant；无需显式注册（graph 名即 deepagents 的 assistant_id）。
_Avoid_: 内置 assistant、系统图

## 配置词表

**Graph key（图名）**:
`aegra.json` 中 `graphs` 的键，同时是这份部署对外暴露的那个默认 assistant 的身份；客户端可以直接把它当 `assistant_id` 用。
_Avoid_: 图 id、模型名

**Graph id**:
assistant 记录指向的图名，值等于它建立时所用的 graph key。key 从配置里删掉后，这条记录的 graph_id 就指向不存在的图。
_Avoid_: 把 graph_id 当 graph key 的别名（一个是记录字段，一个是配置里的键）

**Assistant id**:
接口里标识 assistant 的字符串：可以是 assistant 的 uuid，也可以是当前配置里的 graph key。两者都不匹配即 404。
_Avoid_: 认为任意字符串都能当 assistant_id 用

**File logging（文件日志）**:
把同一份结构化日志同时写到 stdout 与本地文件的出口；文件恒为 JSON，带大小轮转与按天清理。
_Avoid_: 把文件日志当成独立于 stdout 的另一套日志

## 用户与空间词表

**Account（账号）**:
一个登录身份，对应 `user_tables.account`，也是 JWT 的 `sub`。
_Avoid_: user_id（aegra 的 `user_id` 是运行时身份，两者在接上 auth handler 前并不相等）、用户 ID

**Microservice（微服务）**:
用户拥有的一项业务。不是存储实体：它与微服务空间一一对应，空间名即微服务名。
_Avoid_: 把它当成数据库里的一张表

**Knowledge space（知识空间）**:
存放全部业务知识的根目录。不是存储实体：它只是微服务空间的容器。根下直接的散文件不属于任何 Space，对所有用户只读共享。
_Avoid_: 知识库、KB、把某个微服务空间当成知识空间

**Space（微服务空间）**:
知识空间下的一个文件夹，对应 `space` 表一行，也是权限的最小单位：一个 Space 承载且仅承载一个微服务。文件夹由 Space 的名字在知识空间根下派生（同名目录），一个账号可拥有多个 Space。
_Avoid_: workspace、业务空间、微服务空间（口语可用，代码与库里一律 Space）

**UserSpace（用户-空间关联）**:
`user_space` 表：哪个账号能看哪个 Space。写入直接操作本表，不用 `UserTables.spaces`（只读视图）。

> **已知不一致**：`graphs.access.spaces.allowed_spaces()` 只认本表的行，而
> `custom.api.spaces._own_space_names()` 对 `is_super` 额外放行“全部 Space”。
> 所以超级用户如果没被分配 `user_space` 行，前端能看到全部 Space，但图里的子 agent 列表为空
> （`Available types:` 空、`check_async_task` 不在工具表）。要么给账号补行，要么把两处规则收敛成一套。
_Avoid_: 把 `user_microservice` 当表名（已改名）

**Space path（空间路径）**:
Space 的 `path` 列，相对项目根的路径（服务启动时的 CWD）。不存绝对路径。当前不参与访问控制——访问控制按 Space 名字派生文件夹。
_Avoid_: 把它当成绝对路径或 URL、把这一列当成访问控制的依据

**Service account（服务账号）**:
async subagent 的运行身份。它不是登录账号，没有关联 Space，子 agent 运行期间以它通过鉴权。
_Avoid_: 把它当成普通用户、把子 agent 当成终端用户本人

**Space agent（空间子 agent）**:
一个 Space 对应的异步子 agent，名字就是 space 名（如 `cbam-reporting`）。它的两份内容都在该空间文件夹里：
`<space>/AGENT.md` 的 frontmatter `description` 决定主管何时派给它，正文就是它的 system prompt。
**没有固定名单**：用户拥有哪些 space 就有哪些子 agent，名单每次 run 现算；
所有 space agent 共用同一个图 `domain_agent`，space 经 run config 绑定。
删除 Space 不影响已派出任务：那些任务保留当时授权的 space，结果照常投递；新任务不会再选到它。
_Avoid_: 把子 agent 的数量或名字写成固定值、把 prompt 写在代码里（它与文档同目录才有单一来源）

**Async task（异步任务）**:
主管经 `start_async_task` 派出的一个后台子 agent 执行。在主管 state 的 `async_tasks` 里登记，
键是 `task_id`，值同时就是子执行所在的 thread id；`agent_name` 是当时的 space 名。
它的 `status` 是**快照**，只由
`check_async_task`、派活前的核实或 run 首尾的投递节点刷新，不保证实时。
`delivered_run_id` 记的是“这个 run 的结果已入上下文”（与发件箱行的“已兑付”区分：前者是
本地的判重位点，丢了能重建；后者才是跨进程的唯一真相）。
一个主管能派多少个并发异步任务**没有业务上限**，它们会在服务端排队（见 Thread quota）。
_Avoid_: 把 `async_tasks.status` 当成子 run 的真实状态（真实状态查 `GET /custom/tasks/{id}`）

**Thread quota（会话配额）**:
一个账号最多拥有多少个 thread（`config.yaml` 的 `quota.max_threads_per_user`）。子线程也计数，但计入它所属的
主管：子线程建时带 metadata `owner_identity`（终端用户）与 `parent_thread_id`，
计数只统计 `parent_thread_id` 为空的那批。超额时 `POST /threads` 返回 403。
删主管线程会连带删它的子线程，否则子线程（属主是服务账号）会成为永久孤儿。
它取代了旧的“一个主管最多 N 个并发子 agent”的限制（ADR-0012）。
_Avoid_: 把它当成限流（它限的是存储与界面规模，计算开销由 worker 队列承接）；
按 `thread.user_id` 数子线程（它们是服务账号）

**Task delivery（任务结果投递）**:
把子 agent 的结果送进主管上下文的动作，只在**模型能反应的时刻**发生，且只发生一次：
① 主管开始一轮 run 时 —— `AsyncTaskGuard.abefore_agent` 把已终态的结果写进 state
（不另开 run，模型在本次 run 的第一轮模型调用就能看到）；② 主管已空闲（`idle` / `error`）——
服务端发件箱分发者 `custom/service/outbox_dispatcher.py` 开一轮唤醒 run 投递。`busy` 与 `interrupted`
一律不投：前者归 ②，后者不能顶掉用户正等着回答的答案（插一轮 run 会把线程从 `interrupted`
改成 `busy`，用户点“回答”时会被 400 拒掉）。**run 期间完成的结果不在该 run 里写**
（没有 `after_agent`）——那时模型这一轮已经答完，写进去它没法转述，交给 ② 另开一轮。
这里的 `busy` 指**父线程上有活跃 run 行**（`runs.status` 为 `pending` / `running`），
不是 `thread.status`：前者是分发者的判据，后者只是给人看的状态字。
两条路都以 `(task_id, run_id)` 为键：子 run 终态时 `finalize_run` 写一行发件箱
`async_task_outbox`（主键即幂等键）。投递前把行从 `pending` 原子翻成 `delivered`——这一步叫
**已兑付**，是跨进程、跨 run 的唯一真相：翻不成就说明别人已投，放弃；**返回不了结果时也不投**
（行没卖掉，下一轮再试，最坏是晚而不是丢）。投过的任务在父 state 里留 `delivered_run_id`，
叫**已入上下文**：它只是已兑付的本地缓存，丢失能重建（最多多一次 claim），所以不能拿它
当唯一依据，更不能拿“消息历史里有没有那张卡片”当依据——摘要会删掉它，模型也能复述伪造它。
模型自己 `check_async_task` 拿到终态结果的，同样记为已入上下文并发起已兑付。
**Status notice（状态通知）**:
把子任务的状态变化（被取消 / 被中断）告诉主管的一条消息，前缀同样是
`[Async subagent`，但**不带 `Result:`**，前端渲染成灰色状态条。它和投递是两条路：
投递送结论、占发件箱行；状态通知只说"这轮没跑完"、不占任何行，判重键是
`async_tasks[tid].notified_run_id`（投递用 `delivered_run_id`）。
_Avoid_: 把状态通知叫投递；把 cancelled 当结果去投（那会把半截内容当结论）

投递消息带子 agent 最后一条消息的**原文**（不截断）；过长只告警不切文本，避免模型把“被切了”
当成“写完了”。
发件箱表 / ack 接口 / 分发者见 `custom/db/models.py`、`custom/api/delivery.py`、
`custom/service/outbox_dispatcher.py`（ADR-0010）。
_Avoid_: 把投递说成“通知”（通知是手段，投递是结果进上下文）；把它当成同步等待（主管从不被阻塞）；
以为发件箱里存了结果正文（它不存，正文靠投递时现查子线程）

**Task proxy（任务代理）**:
`custom/api/` 里把子 agent 的线程状态与事件流转给浏览器的接口。子 agent 的 thread
属主是服务账号，终端用户直接读会被 ownership 过滤挡成 404，所以必须经代理。
_Avoid_: 把它当成转发层（它同时是授权层：要求父线程属于当前用户且其 `async_tasks` 里有该 task）

## 代码布局

三个顶层包，依赖方向单向（由 `tests/unit/test_layout.py` 守护）：

- `custom/` —— **HTTP 面**。`app.py` 装配（`aegra.json` 的 `http.app`）；
  `api/` 只做 HTTP 形状（一个 URL 域一个文件）、`service/` 业务、`db/` ORM 与仓储、
  `middleware/` 中间层（Auth handler）。
- `graphs/` —— **图面**，只在 run 里跑。根目录只放 `aegra.json` 直指的入口
  （`supervisor.py`、`space_agent.py`、`xlsx_agent.py`）；`middleware/` 中间层（AgentMiddleware）、
  `access/` 横切的访问判定（spaces 列表、带 ACL 的 backend、Mirage 后端与 Workspace 池）、
  `tools/` 与 `prompts/`。
- `shared/` —— **两侧共用的纯逻辑**：`config`（config.yaml 单例）、`token`（JWT 铸造）、
  `outbox`（投递文本口径）、`session/`（Store 与对象存储底座、上传 key 布局与
  上传路由规则 `upload_rule`）、`knowledge/`（知识根、`AGENT.md` 解析与脚手架）。

规则：`custom → shared`、`graphs → shared`，**两个包互不 import**；`shared` 不 import
FastAPI / LangChain / deepagents。（上传功能移除后已无任何例外；旧的 xlsx→custom 懒加载已删。上传读路径经 Mirage 在
`graphs/access/mirage_backend.py`，上传写路径在 `custom/api/uploads.py`，规则见 ADR-0018。）

数据不在代码包里：空间知识在仓库根 `knowledge/`（`config.yaml` 的 `okf.dir`）。
_Avoid_: 往 `graphs/` 里放数据；在 `custom/` 里放图侧要用的模块（要放就放 `shared/`）；
在两个包根目录新增文件（根目录只放入口）
