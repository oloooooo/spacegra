# 前端接口参考

前端（独立仓库，Vue 3 + Vite）对接本服务时的接口契约。全部结论来自**实测**（真起服务、跑 supervisor 与三个领域子 agent）。

实现位置：`custom/api/`，一个 URL 域一个文件——`account.py`（注册 / 登录 / whoami）、`graphs.py`（图清单）、`spaces.py`（空间与文件树）、
`tasks.py`（任务状态 / SSE 流 / 取消）、`delivery.py`（投递 ack）；路由前缀都是 `/custom`。除了这一层，其余接口直接用 LangGraph 原生端点（见 §4）。

另有 **3 个内部接口**：

| 接口 | 调用方 | 说明 |
|---|---|---|
| `POST /custom/tasks/{task_id}/deliver-claim` | 主管侧拦截层 / 分发者 | 把发件箱行从 `pending` 原子翻成 `delivered`（[adr/0010](./adr/0010-task-delivery-idempotency.md)） |
| `DELETE` 同名 | 同上 | 撤销认领 |
| `POST /custom/tasks/{task_id}/cancel` | **前端**（侧栏任务卡「取消」） | 取消一个子任务。子线程属主是服务账号，用户 token 打不通原生 `cancel`，所以后端代理一次（[adr/0014](./adr/0014-no-hitl-for-eval.md)） |

前者鉴权只允许父线程属主或服务身份；`cancel` 同样。

## 1. 为什么需要这一层

| 原生接口的问题 | 本层怎么解 |
| - | - |
| `GET /assistants` 返回全部历史 assistant（实测 14 条），含已删除的图与示例图 | `GET /custom/graphs` 只回 `aegra.json` 里注册的图 |
| 子 agent 跑在独立 thread，属主是服务身份 `subagent_service`，终端用户 `GET /threads/{子线程}/state` 被 ownership 过滤挡成 **404** | `GET /custom/tasks/{id}` 与 `/stream` 做带鉴权的代理（ADR-0007） |
| Space 内容没有任何接口暴露 | `GET /custom/spaces`、`/spaces/{name}/files` |
| 成员管理只有「建 / 分配」，没有查 / 移除 | `GET/DELETE /custom/spaces/{name}/users` |

## 2. 鉴权

所有接口都要 `Authorization: Bearer <token>`，token 来自：

```http
POST /custom/login
{"identity": "root", "password": "1234"}

200 {"access_token": "...", "token_type": "bearer", "expires_in": 3600}
```

- token **1 小时**过期（`config.yaml` 的 `auth.access_token_ttl`），没有 refresh token；前端收到 401 就回登录页。
- `permissions` 含 `write` 表示超级用户（`user_tables.is_super`），能建 Space、分配用户、看全部 Space。
- 当前身份：`GET /custom/whoami`；自己的空间：`GET /custom/user/spaces`。

## 3. 本层接口

### 3.1 `GET /custom/graphs`

前端可选的助手。

```json
{"graphs": ["domain_agent", "supervisor"],
 "identity": "root", "permissions": ["read", "write"]}
```

`graph_id` 可以直接当 `assistant_id` 用（见 `CONTEXT.md` 的 Graph key 词条）。
现在只有两个图：所有领域子 agent 共用 `domain_agent`，space 经 run config 绑定（ADR-0011）。

### 3.2 `GET /custom/spaces`

```json
{"is_super": true, "spaces": [
  {"name": "cbam-reporting", "path": "knowledge/cbam-reporting", "accessible": true, "file_count": 6}
]}
```

`accessible` 已合并超管判定（超管对全部 Space 为 `true`）。

### 3.3 `GET /custom/spaces/{space_name}/files`

```json
{"space": "cbam-reporting", "root": "knowledge/cbam-reporting", "files": [
  {"path": "/cn_goods_rules.xlsx", "size": 7332, "modified": "2026-10-18T03:50:49+00:00", "ext": ".xlsx"}
]}
```

无权限 → `403`。路径为 Space 内相对路径，直接拼 `/空间名/相对路径` 就是给 agent 用的虚拟路径。

### 3.4 `GET /custom/spaces/{space_name}/users` / `DELETE .../users/{identity}`

```json
{"space": "cbam-reporting", "users": ["root"]}
```

删除成功 → `{"space": ..., "removed": "..."}`；不在空间里 → `404`；非超管 → `403`。

### 3.5 `GET /custom/tasks/{task_id}?parent_thread_id={父线程}`

异步子任务状态。`task_id` 即父线程 state 里 `async_tasks` 的键（同时就是子线程 id）。

```json
{"task_id": "fe2bb1b4-...", "thread_id": "fe2bb1b4-...", "run_id": "e1c957e1-...",
 "agent_name": "hybrid-bonding", "status": "running",
 "created_at": "...", "last_checked_at": "...", "message_count": 7}
```

`agent_name` 是**空间名**（即用户创建的那个 Space，见 ADR-0011）。

- `status` 是**向子 run 核实过的**真实状态，不要读父线程 state 里的 `async_tasks.status`（会滞后，见 ADR-0007）。
- 授权：父线程必须属于当前用户，且它的 `async_tasks` 里确实有这个 `task_id`；否则 `404`。

### 3.6 `GET /custom/tasks/{task_id}/stream?parent_thread_id={父线程}`

把子 agent 的 run 事件转成 SSE。SSE 帧格式与主对话流一致（`event:` + `data:`），前端可复用同一个解析器。

帧序列：

```
event: metadata   data: {"task_id": "...", "thread_id": "..."}
event: values     data: {"messages": [...]}          ← 连接前的快照（补齐历史）
event: messages/partial | custom | messages/complete ← 实时
event: end        data: {"status": "success"}
```

实测一次子 agent 执行的事件量：`messages/partial ×3418`、`custom ×18`（工具 start/done）、`messages/complete ×9`、`end ×1`。
**必须节流**：`messages/partial` 是逐 token 全量文本，直接渲染会卡。

- 任务已结束时：只发 `metadata` + `values` + `end`，不发实时帧。
- `stream_mode` 查询参数可覆盖，默认 `messages,custom`；`values` 每步都是整态快照，量大，要才加。

前置条件：子 run 创建时必须声明 `stream_mode=["messages","custom"]`（`graphs/middleware/async_task_guard.py` 已声明）。若不声明，aegra 默认只产 `values`，代理订阅不到 token 流。

### 3.7 `POST /custom/uploads`

上传一个文件到当前会话（`multipart/form-data`，字段 `file` + `thread_id`）。
字节进对象存储的 `uploads/<上传者>/<会话>/<文件名>`，同名即覆盖；台账行记归属与投递目标
（ADR-0018）。只能往自己的会话传：线程不存在 404、不是自己的 403。

```json
{
  "name": "cbam-reporting_碳排放.xlsx",
  "size": 20480,
  "sha256": "…",
  "target_agent": "cbam-reporting",   // 空间名 / "xlsx" / "main"
  "virtual_path": "/uploads/cbam-reporting_碳排放.xlsx"
}
```

- `target_agent` 在上传那一刻算好：`<空间名>_xxx.xlsx` 且空间在你的授权内 → 该空间的子 agent；
  其他 xlsx（含写了别的空间名、你却没有权限的）→ 通用 `xlsx` 子 agent；md 等 → 主 agent 自己读。
- 错误码：`413` 单文件超上限（表格按 `xlsx.max_blob_mb`，其余按 `session_files.max_upload_mb`）、
  `409` 会话文件数超 `session_files.max_files_per_session`、`422` 文件名带路径分隔符、
  `503` 对象存储不可用。
- 上传后 agent 不需要刷新：它的文件系统就是对象存储，下次 `ls /uploads` 就能看见。

## 4. 直接用的原生接口

| 用途 | 接口 | 备注 |
| - | - | - |
| 会话列表 / 建 / 删 | `POST /threads/search`、`POST /threads`、`DELETE /threads/{id}` | 已按 `user_id` 隔离；删除不可恢复。**建会话有配额（超额 403）**，删会话会连带删子线程，见 §7 |
| 发消息 + 流式 | `POST /threads/{id}/runs/stream` | `stream_mode=["messages","custom","updates","values"]` |
| 追问恢复 | `POST /threads/{id}/runs` | body `{"command": {"resume": "<选项 label>"}}` |
| 停止 | `POST /threads/{id}/runs/{run_id}/cancel` | |
| 历史 / 状态 | `GET /threads/{id}/history`、`GET /threads/{id}/state` | |
| 定时任务 | `POST /threads/{id}/runs/crons`、`POST /runs/crons/search`、`PATCH/DELETE /runs/crons/{id}` | 见 §6 |

用不到的：`/store/*`、`/runs/crons` 之外的 stateless runs、`/assistants/*/subgraphs`、checkpoint 回放。

## 5. 前端必须处理的四个契约

### 5.1 `messages/partial` 要自己算增量

aegra 推的每条 `messages/partial` 都是「该消息到目前为止的**完整内容**」，不是新增片段：

```js
if (text.length > printed[msg.id] ?? 0) { render(text.slice(printed[msg.id] ?? 0)); printed[msg.id] = text.length }
```

### 5.2 追问（HITL）

`updates` 流里出现：

```json
{"__interrupt__": [{"id": "6dbb...", "value": {
  "tool": "ask_user_question", "header": "选表",
  "question": "\"那个表\"指哪一个？",
  "options": [{"label": "键合工艺DOE.xlsx", "description": "..."}]}}]}
```

渲染成选项卡片 → 用户点选 → `POST /threads/{id}/runs` `{"command":{"resume":"<label>"}}`。
agent 收到的工具结果形如 `{"answer":"键合工艺DOE.xlsx","extra":null}`。一次回答后它可能接着问下一个问题，要支持连续中断。

### 5.3 子 agent 的结果不是用户消息

子 agent 完成后，父线程会多一条 **`human`** 消息，内容以 `[Async subagent 'cbam-reporting' has completed] Result: ...`
开头，末尾带 `task_id: <id>` 与 `run_id: <id>`。它有两种来源，**格式完全一致**，前端不需要区分
（判重在服务端做，见 `POST /custom/tasks/{id}/deliver-claim`）：

- 主管的 run 开始 → `AsyncTaskGuard.abefore_agent` 写进自己的 state（不开新 run）；
- 主管已答完（线程 `idle`）→ 服务端发件箱分发者 `custom/service/outbox_dispatcher.py` 开一轮唤醒 run
  （ADR-0010；不再有子 agent 侧的通知中间件，也不需要现铸父身份 token）。

两种都**渲染成「子任务返回结果」区块**，不要当作用户输入。主管在等用户回答（线程 `interrupted`）时
不会投递，结果会等用户答完后补上——前端不要为此做特殊处理。结果在主管**run 期间**完成时
不会插进那一轮，而是等 run 结束后由分发者开一轮唤醒 run 送来（多一条独立回复）。

**结果正文不截断**：消息里就是子 agent 最后一条消息的原文，可能很长（几千字）。前端建议
默认折叠成“展开查看全文”，否则一条卡片会占满整屏。超过 3000 字服务端会打
`task_result_long` 告警，但消息内容不做任何删减。

**状态通知是另一种消息**：前缀同样以 `[Async subagent` 开头，但**不含 `Result:`**：

```
[Async subagent 'cbam-reporting' was cancelled]

task_id: …
run_id: …
```

它表示"这一轮没跑完，没有结果"，渲染成灰色状态条，不要当结论展示（`web/src/stores/chat.ts`
的 `toView` 用是否含 `Result:` 区分 `callback` / `notice` 两种 role）。

### 5.4 状态可能滞后

父 state 的 `async_tasks.status` 只在主管调用 `check_async_task`、或者主管 run 开始
（`AsyncTaskGuard.abefore_agent`）核实时更新；即使子 agent 已完成、
线程已 `idle` 也没人改它。要展示实时状态就用 `GET /custom/tasks/{id}`（内部会向子 run 核实）。

## 6. 定时任务

```http
POST /threads/{thread_id}/runs/crons
{"assistant_id": "supervisor", "schedule": "* * * * *", "timezone": "Asia/Shanghai",
 "input": {"messages": [{"role": "user", "content": "每日巡检"}]}}
```

实测行为：

- **创建即先跑一次**，之后按 cron 重复；返回的是一次 Run，`status` 从 `pending` 开始。
- cron 触发的 run **带完整用户身份**：派子 agent、完成回写、Space 权限全部正常
  （日志里能看到 `outbox_wake_run_created` / `task_results_delivered`）。
- `POST /runs/crons/search` 返回 `next_run_date` / `enabled` / `payload`；`PATCH` 可改 schedule 或 `{"enabled": false}` 暂停。
- **删除线程会级联删除它的 cron**，不会留孤儿。
- 没有「执行历史」接口：每次触发的 run 就在该线程的 `/threads/{id}/runs` 里。

## 7. 会话删除

`DELETE /threads/{id}` → `200`；之后 `GET /threads/{id}` → `404`，`/threads/{id}/runs` → `[]`。
无软删除、无回收站，前端必须二次确认。

**服务端会级联删子线程**（ADR-0012）：子 agent 的线程属主是服务账号，用户看不见也删不掉，
所以删主管会话时一并清理它们（含 checkpoint）。后果：一次删除可能比过去慢（每个子线程一次
`adelete_thread`），前端不要假定删除请求毫秒级返回。

### 建会话的配额

`POST /threads` 每个账号最多 `quota.max_threads_per_user`（`config.yaml`，默认 50）个会话（子线程也计名额，一个会话派 3~5 个子任务就吃掉 3~5 个）。
超额返回：

```http
403 {"error": "forbidden", "message": "会话数已达上限（50），请先删除不用的会话再新建", "details": null}
```

前端拿 `message` 直接提示即可（`client.ts::errorText` 已优先读它），不要当成登录失效。
超管（`permissions` 含 `write`）不受限。删掉一个会话就能再建一个（级联删子线程后名额释放）。

## 8. 已知限制

- 代理接口是**单次 Run 订阅**：子 agent 若被 `update_async_task` 再跑一轮，前端要按新的 `run_id` 重新连。
- `service_token()` 是 10 年期长期 token（沿用 ADR-0004 的 shortcut），只在服务端使用，不下发浏览器。
- 没有把文件写进 Space 的接口：空间知识只能手工放盘 / `scripts/okf_push_s3.py`（见 [S3.md](./S3.md)）。
  会话上传走 §3.7 的 `POST /custom/uploads`（字节进对象存储）。
- 没有 refresh token。
