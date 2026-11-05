# ADR 索引

**ADR 编号永不复用**：代码注释里写着 `ADR-0013` 这类编号，改布局、合文档都不改号——
要查某条决定，先在这张表里按编号找文件。

- 本目录：**仍然约束当前代码**的决定（8 篇）
- [`archive/`](./archive/)：历史决定（8 篇）——被取代、或已被实现固化/被别处文档覆盖

状态行是唯一必须跟着代码更新的字段；ADR 正文里的模块路径、env 名、行号都是**当时的布局**，
别照着找文件（当前布局见 [`../../CONTEXT.md`](../../CONTEXT.md) 的「代码布局」）。

## 现行（8）

| # | 决定 | 一句话 | 代码落点 |
|---|---|---|---|
| [0004](./0004-static-service-identity-for-async-subagents.md) | 静态服务身份 | 子 agent 用构建期铸的长效 token 跑，`custom` 的 Auth handler 对它免查库放行 | `shared/token.py`、`custom/middleware/auth.py` |
| [0005](./0005-spaces-forwarded-via-run-config.md) | spaces 经 run config 转发 | 子 agent 只拿到"本任务那一个 space"，权限收窄不在子 agent 里判 | `graphs/access/spaces.py`、`graphs/middleware/async_task_guard.py` |
| [0007](./0007-frontend-access-to-async-tasks.md) | 子 agent 的流/状态经代理暴露 | 子线程属主是服务账号，浏览器读不到，所以带鉴权代理一次，不放开 ownership | `custom/api/tasks.py` |
| [0010](./0010-task-delivery-idempotency.md) | 事务性发件箱 + 单一分发者 | 结果投递只有一条写入路径：`pending → delivered` 单行条件 UPDATE 就是幂等键 | `custom/service/outbox_dispatcher.py`、`custom/api/delivery.py`、`graphs/middleware/async_task_guard.py` |
| [0011](./0011-space-agent-is-dynamic.md) | 空间子 agent 动态化 | 一个 `domain_agent` 图服务所有 space，名单每次 run 现算，加空间不改代码 | `graphs/supervisor.py`、`graphs/space_agent.py`、`shared/knowledge/spec.py` |
| [0012](./0012-thread-quota-replaces-task-limit.md) | thread 配额取代派活上限 | 限的是"账号能有几个会话"（子线程计入所属主管），不是并发算力 | `custom/service/quota.py`、`custom/middleware/auth.py` |
| [0016](./0016-spaces-in-object-storage.md) | 空间知识进 S3 | `okf.backend: local\|s3\|s3fs` 三选一：`s3` 读本地镜像（不重写 14 个 backend 方法），`s3fs` 由 Mirage 直读（ADR-0018） | `shared/knowledge/root.py`、`scripts/okf_{push,pull}_s3.py` |
| [0018](./0018-uploads-and-reads-through-mirage.md) | 上传重建：字节与读路径都可切，集群走 Mirage | `uploads/<owner>/<thread>/` + `okf/` 前缀；每 run 一个 Mirage Workspace（TTL 池），可见性用 profile 白名单，不继承 `SandboxBackendProtocol`；表格解析一次存 Parquet 走 DuckDB；**单机可用 `uploads.store: fs` + `LocalBackend`，不需要对象存储**（§9） | `graphs/access/mirage_backend.py`、`graphs/access/uploads_backend.py`、`shared/session/uploads.py`、`graphs/xlsx_agent.py`、`custom/api/uploads.py`、`custom/service/uploads.py`、`shared/session/upload_rule.py` |

## 已归档（8）

| # | 决定 | 为什么归档 |
|---|---|---|
| [0001](./archive/0001-acp-lives-in-aegra-api.md) | ACP 内置于 aegra-api，不抽独立包 | 定位性结论，已完成，不再约束代码 |
| [0002](./archive/0002-status-and-cancel-vocabulary.md) | 不引入 `cancelled`；取消与 HITL 共用 `interrupted` | 词表已进 `CONTEXT.md`（Run status / Cancel action / Supersede），日常查词表即可 |
| [0003](./archive/0003-multitask-strategy-interrupt-supersede.md) | `multitask_strategy="interrupt"` 真执行 Supersede | 已实现并固化进代码与 e2e（`tests/e2e/test_runs/test_supersede_e2e.py`） |
| [0006](./archive/0006-completion-notify-mints-parent-token.md) | ~~完成通知现铸父身份 token~~ | **被 [ADR-0010](./0010-task-delivery-idempotency.md) 取代** |
| [0008](./archive/0008-active-task-limit-is-per-process.md) | ~~派活并发上限只在单进程成立~~ | **被 [ADR-0012](./0012-thread-quota-replaces-task-limit.md) 取代** |
| [0009](./archive/0009-worker-mode-requires-redis-broker.md) | 多副本 / worker 必须开 Redis broker | 结论与实测证据已并入 [`../configuration.md`](../configuration.md) §3.1「Redis 与 worker」 |
| [0014](./archive/0014-no-hitl-for-eval.md) | 不做 eval 的 HITL 审批 | 反决定已固化：取消不占发件箱行、不唤醒主管，代码注释与 [`../design.md`](../design.md) 都有说明 |
| [0017](./archive/0017-local-backend-scandir.md) | 本地 backend 一趟 scandir | 纯实现/性能决定，已固化进 `LocalBackend` 与 `tests/unit/test_local_backend.py`；现行读路径见 0018 |

## 按主题该读哪几篇

| 想了解 | 读 |
|---|---|
| 谁能看哪个 space、子 agent 拿到什么权限 | 0005 → 0004 → 0011 |
| 子任务结果怎么回到主管、为什么不会投两次 | 0010（+ [0014](./archive/0014-no-hitl-for-eval.md) 讲"取消不是结果"） |
| 前端怎么看子任务进度、为什么不能直连 | 0007（+ [0014](./archive/0014-no-hitl-for-eval.md)） |
| 空间知识放哪、多副本怎么读同一份 | 0016 |
| 上传文件放哪、agent 怎么读、谁能看见谁 | 0018（§9 讲单机本地模式） |
| 表格怎么查、为什么不用 SQLite | 0018 §8 |
| 账号能开多少会话 | 0012 |
| 部署成多副本要注意什么 | [`../configuration.md`](../configuration.md) §3.1（原 0009） |
