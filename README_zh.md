# 启动指南（中文）

从零把 aegra-api（后端）+ `web/`（前端）跑起来，并做出第一次「主管 → 领域子 agent」对话。

> 全部命令都在**仓库根目录**执行，除非注明 `cd web`。
> 环境：Windows / macOS / Linux 均可；Windows 相关坑见 [§8](#8-常见问题)。

---

## 1. 这个项目是什么

- **后端**：自托管的 Agent Protocol 服务（`src/aegra_api`），在 `aegra.json` 里注册图。
- **图**（`graphs/`）：
  - `supervisor` — 主 agent，负责理解意图、拆清单、把活派给领域子 agent；
  - `supervisor` + `domain_agent` — 主 agent 与**唯一一个**领域子 agent 图；后者按 run config 里的 space 现读 prompt，所以用户有几个空间就有几个子 agent（不用改代码）；
  - `xlsx_agent` — 通用表格子 agent：读 `/uploads` 里不属于任何空间的表（主 agent 自己没有表格工具）。
- **上传文件**：`POST /custom/uploads` 把文件挂到**当前会话**的 `/uploads`（md 归主 agent 自己读，xlsx 在派活时转给对应子 agent）。字节存哪儿由 `uploads.store` 决定：单机默认 `fs`（本机 `data/uploads/`），多副本用 `s3`（对象存储）。
- **知识空间**：`knowledge/<空间名>/`，每个空间是一份 OKF（Markdown + YAML frontmatter，可带 xlsx 表格）。
- **前端**（`web/`）：Vue 3 + Vite，左边会话、中间对话、右边面板（子任务实时进度 / 知识库文件树 / 定时任务）。
- **代码布局**：`custom/`（HTTP 面：`app.py` + `middleware/ api/ service/ db/`）、`graphs/`（图面：入口 `supervisor.py` / `space_agent.py` / `xlsx_agent.py` + `middleware/ access/ tools/ prompts/`）、`shared/`（两侧共用的纯逻辑：`config token lifecycle outbox session/ knowledge/`），`knowledge/` 只放数据。依赖单向：两个包都只依赖 `shared/`，互不 import。详见 [`CONTEXT.md`](./CONTEXT.md) 的「代码布局」。

---

## 2. 前置条件

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | ≥ 3.12 | `pyproject.toml` 的 `requires-python` |
| [uv](https://docs.astral.sh/uv/) | 最新 | 依赖与虚拟环境 |
| PostgreSQL | 13+ | 持久化 thread/run/checkpoint，**必须有** |
| Redis | 6+ | `.env` 开了 `REDIS_BROKER_ENABLED=true` 就需要（默认配置就是开）：跨进程取消 + worker 队列。没有 Redis 就把该开关设回 `false` 并**只用单进程**启动（ADR-0009） |
| Node.js | ≥ 18（实测 24） | 只有前端需要 |

```bash
python --version && uv --version && node -v
psql --version   # 或确认 PG 已在 localhost:5432 监听
```

---

## 3. 配置：`.env` + `config.yaml`

```bash
cp .env_example .env
```

两份文件分工（逐键说明见 [`docs/configuration.md` §3](./docs/configuration.md#3-配置来源)）：

| 文件 | 放什么 |
|---|---|
| `.env` | aegra 原生运行参数（数据库、端口、日志…）+ **密钥** + 第三方模型 key |
| `config.yaml` | 本项目新增的非密钥参数（会话配额、单个文件上限、上传介质、对象存储地址、知识空间）—— 已有默认值，不改也能跑 |

`.env` 至少改这几项：

```ini
# --- A. aegra 原生：数据库 ---
POSTGRES_USER=postgres
POSTGRES_PASSWORD=你的密码
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=aegra          # 库要先建好：createdb aegra

# --- A. aegra 原生：服务 ---
HOST=127.0.0.1
PORT=2026
AEGRA_CONFIG=aegra.json

# --- B. 密钥：两个必须设置，否则启动就报错 ---
# 生成：python -c "import secrets;print(secrets.token_urlsafe(48))"
JWT_SECRET=换成长随机串
PASSWORD_PEPPER=换成长随机串
# （旧版还有 SESSION_SCOPE_SECRET：上传重建后已不再读取，可删）

# --- C. 第三方：模型（OpenAI 兼容接口，graphs/model.py 读取） ---
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com      # 用官方则可留空
OPENAI_MODEL=deepseek-flash
```

`config.yaml` 只改真要调的（示例：一个账号最多几个会话；生产接对象存储）：

```yaml
quota:
  max_threads_per_user: 50   # 含子线程；一个会话派 3~5 个子任务就吃掉 3~5 个名额
s3:
  endpoint: http://127.0.0.1:8333   # 留空 = 不启用对象存储（okf.backend 只能填 local）
```

> `PASSWORD_PEPPER` 一旦改动，**已注册的账号全部无法登录**（bcrypt 哈希不再匹配），需要重新注册或重置密码。
> 改了 `config.yaml` 或 `.env` 都要**重启进程**；同名环境变量优先于 `config.yaml`。

建库（若还没有）：

```bash
createdb -U postgres aegra
# 或：psql -U postgres -c "CREATE DATABASE aegra;"
```

---

## 4. 安装依赖 + 建表

```bash
uv sync                 # 装 Python 依赖（含 deepagents / langgraph / pandas / openpyxl）
uv run alembic upgrade head
```

`alembic upgrade head` 会建出 `thread` / `run` / `checkpoint*` / `space` / `user_tables` / `user_space` 等表。

---

## 5. 初始化账号与知识空间

`/custom/register` 只能注册**普通用户**，而「建空间 / 分配空间」需要超级用户。所以第一次要按下面顺序做：先注册账号，再用 SQL 提权并分配空间。

### 5.1 先起服务（见 §6），然后在另一个终端：

```bash
curl -X POST http://127.0.0.1:2026/custom/register \
  -H 'Content-Type: application/json' \
  -d '{"identity":"root","display_name":"root","password":"1234"}'
# → {"message":null,"user_identity":"root"}
```

### 5.2 提权 + 登记三个知识空间

```bash
psql -U postgres -d aegra <<'SQL'
-- 1) 超管（只有超管能建空间 / 分配空间）
update user_tables set is_super = true where identity = 'root';

-- 2) 登记知识空间（path 相对项目根，不是绝对路径）
insert into space(name, path) values
  ('cbam-reporting', 'knowledge/cbam-reporting'),
  ('hybrid-bonding', 'knowledge/hybrid-bonding'),
  ('sbol3-synbio',   'knowledge/sbol3-synbio')
on conflict (name) do nothing;

-- 3) 把空间分配给 root（子 agent 只能看到派给它的那一个）
insert into user_space(identity, space_name) values
  ('root','cbam-reporting'),
  ('root','hybrid-bonding'),
  ('root','sbol3-synbio')
on conflict do nothing;
SQL
```

### 5.3 验证

```bash
curl -s -X POST http://127.0.0.1:2026/custom/login \
  -H 'Content-Type: application/json' \
  -d '{"identity":"root","password":"1234"}'
# 拿到 access_token 后：
curl -s http://127.0.0.1:2026/custom/whoami -H "Authorization: Bearer <token>"
# → {"identity":"root", ..., "permissions":["read","write"]}

curl -s http://127.0.0.1:2026/custom/user/spaces -H "Authorization: Bearer <token>"
# → {"space":["cbam-reporting","hybrid-bonding","sbol3-synbio"], ...}
```

> 更省事的方式：直接用仓库自带的 `custom_test.py`（它会自己登录、跑一轮、等子 agent 回写）：
> ```bash
> uv run python custom_test.py "CBAM 默认排放因子里铝的因子是多少？"
> ```

---

## 6. 启动后端

```bash
# 指定 PYTHONPATH：custom/ 是仓库根的本地包，脚本方式启动时不在 sys.path 里
PYTHONPATH=. uv run python -u src/aegra_api/main.py
```

看到这两行就算成功：

```
Custom app loaded successfully
Uvicorn running on http://127.0.0.1:2026
```

验证：

```bash
curl -s http://127.0.0.1:2026/health
# {"status":"healthy","database":"connected","langgraph_checkpointer":"connected","langgraph_store":"connected"}
```

开发时想自动重载：

```bash
PYTHONPATH=. uv run uvicorn aegra_api.main:app --port 2026 --reload \
  --loop "asyncio:SelectorEventLoop"
```

> Windows 必须显式 `--loop "asyncio:SelectorEventLoop"`（psycopg/asyncpg 不兼容默认的 Proactor 循环）；
> `src/aegra_api/main.py` 里已经处理好了，所以直接用它最省心。

### 6.1 单进程 vs 多 worker

| 形态 | 启动方式 | 影响 |
|---|---|---|
| 单进程（默认） | `python src/aegra_api/main.py` | 一切语义严格：取消、会话配额都准确 |
| 多 worker | `uvicorn aegra_api.main:app --host 127.0.0.1 --port 2026 --workers 2 --loop asyncio:SelectorEventLoop`（需 Redis） | 吞吐上来；会话配额存数据库，多 worker 下仍然准确（ADR-0012） |

两者都依赖 `REDIS_BROKER_ENABLED`：开了走 Redis worker 队列（可多进程、跨进程取消可靠），
关了走进程内执行器（只能单进程，多进程下取消约 50% 失效，见 [adr/0009](./docs/adr/archive/0009-worker-mode-requires-redis-broker.md)）。

### 6.2 启动 Redis（多 worker 必需）

```bash
# 第一次：创建容器（与 .env 的 REDIS_URL=redis://127.0.0.1:6379/0 对应）
docker run -d --name redis6 -p 6379:6379 redis:6-alpine
# 之后每次（Docker Desktop 重启/休眠后）
docker start redis6
docker exec redis6 redis-cli ping   # 应返回 PONG
```

> Docker Desktop 重启或休眠后容器可能退出（`Exited`）。多 worker 模式下 Redis 一挂，
> 新 run 入不了队、子 agent 完成时的唤醒 run 也建不出来（表现：子 agent 返回了但主管不说话，
> 死锁到用户再发一条消息）。启动后端前先 `docker ps` 确认 redis6 是 `Up`。

> **改了 `aegra.json` 一定要重启**：图注册表在启动时读取一次。

---

## 7. 启动前端

```bash
cd web
npm install
npm run dev          # http://localhost:5273
```

- 默认连 `http://127.0.0.1:2026`，要改就 `cp .env.example .env` 后编辑 `VITE_API_BASE`。
- 用 `root / 1234` 登录。
- 后端已开 CORS（`Access-Control-Allow-Origin: *`），前后端不同端口不需要代理配置。

不想开浏览器、只想验证接口契约：

```bash
cd web && node scripts/smoke.mjs      # 20 个断言：登录→建会话→流式→追问 resume→任务代理→定时任务→删会话
```

---

## 8. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `ModuleNotFoundError: No module named 'custom'` | 用脚本方式启动时没带 `PYTHONPATH=.`。加上，或改用 `uvicorn aegra_api.main:app` |
| `KeyError: 'JWT_SECRET'` / `'PASSWORD_PEPPER'` | `.env` 缺这两个键，补齐后重启 |
| `RuntimeError: Database not initialized` | 用了 `get_session_maker()` 但没走服务生命周期；只在服务内可用 |
| `psycopg_pool.PoolTimeout: couldn't get a connection` | PG 没起 / 连接数打满 / 密码错。先 `psql` 手动连一次确认 |
| 登录 401「账号或密码错误」 | 账号没注册，或 `PASSWORD_PEPPER` 被改过 |
| `用户没有空间 'xxx' 的权限，无法启动该子 agent` | 该账号没有这个 Space 的 `user_space` 行；按 §5.2 分配 |
| 建空间接口 403「只有超级用户可以创建微服务空间」 | 账号 `is_super=false`；按 §5.2 提权 |
| 前端登录后一片空白 | 后端没起 / `VITE_API_BASE` 指错；浏览器控制台看 `Failed to fetch` |
| 改了 `graphs/` 下的图或 `aegra.json` 但没生效 | 后端启动时只读一次注册表，**重启服务** |
| 子 agent 一直显示 `running` | 父 state 里的状态是快照（只在 check 时更新）。看真实状态用 `GET /custom/tasks/{id}` |
| 主管说「`check_async_task` 不是有效的工具」/ `Available types:` 为空 | 那一轮 run 的 `user_spaces` 为空 → deepagents 不挂 `AsyncSubAgentMiddleware`（`specs` 空）。检查该账号在 `user_space` 表里有没有行（超级用户也要有）；唤醒 run 的 spaces 由 `custom/service/outbox_dispatcher.py::_parent_user` 现查库 |
| 子 agent 返回的卡片只有 `Result: (no output)` | 唤醒 run 取不到子线程正文。日志出现 `outbox_summary_fetch_failed` = 服务自调（`127.0.0.1:{PORT}`）失败或子线程不存在；没有该日志但仍是空 = specs 为空导致 guard 拿不到摘要。卡住时可让主管直接读空间文件（`ls` / `read_file`） |
| 子 agent 返回后主管不说话（死锁） | Redis 挂了：唤醒 run 同样要入队才能执行。`docker ps` 看 redis6，按 §6.2 启动。旧版（notifier）只试一次所以会永久丢，现版会退避重试，Redis 恢复后自动补投 |
| 端口 2026 被占 | `netstat -ano \| grep :2026` 找到 PID 后结束，或改 `.env` 的 `PORT`（前端同步改 `VITE_API_BASE`） |
| 启动报 Redis 连接失败 / `RuntimeError: Redis not initialized` | `.env` 开了 `REDIS_BROKER_ENABLED=true` 但 Redis 没起：按 §6.2 启动（`docker start redis6`），或把该开关设回 `false` 并只用单进程启动 |
| 取消后 run 还在继续跑 | 多进程启动但没开 Redis：取消信号跨不了进程（ADR-0009）。开 Redis 或改单进程 |
| 建会话报 403「会话数已达上限」 | 每个账号默认最多 50 个会话（`config.yaml` 的 `quota.max_threads_per_user`，ADR-0012；**含子线程**，一个会话派 3~5 个子任务就要吃掉 3~5 个名额）；删掉不用的会话即可，删会话会连带删它的子线程 |
| 上传返回 503「上传存储暂不可用」 | `uploads.store` 是 `s3` 但 `s3.endpoint` 没配 / 对象存储没起。单机把它设成 `fs`（或留 `auto` 且不配 endpoint） |
| 上传成功但 agent 说 `/uploads` 是空的 | `okf.backend: s3fs` 只能配 `uploads.store: s3`（启动时 `uploads.validate()` 会报错）；另外确认问的是**同一条会话**，`/uploads` 按会话隔离 |

---

## 9. 一次完整对话长什么样

1. 在中间输入框问：「CBAM 默认排放因子里铝的因子是多少？」
2. 主管先 `write_todos` 拆任务 → `start_async_task` 派给 `cbam-reporting` → 回你一句「已派给 CBAM 领域 agent（task_id: …）」。
3. 右侧「任务」面板出现一张卡：状态从 `running` → `success`，卡片里能看到子 agent 的实时输出与工具步骤（`xlsx_info` / `xlsx_query`）。
4. 子 agent 跑完，结果会被送到主管面前：主管那次 run 的开始/结束时由 `AsyncTaskGuard` 写进它的 state（不打断、不开新 run）；主管已经答完、线程空闲则由服务端的**发件箱分发者**（`custom/service/outbox_dispatcher.py`）开一轮新 run 把它叫醒（失败/崩溃会退避重试，不会丢）。所以你不用手动催「跑完了吗」；结果消息带的是子 agent 的**完整最后一条消息**，不用再让主管去取。

## 10. 父子 agent 之间的信息流通

父（主管）与子（领域 agent）跑在**两个不同的 Thread** 上，记忆互不相通。它们之间只有下面 5 条通道，没有任何其它隐式共享。

```
┌─────────────────────── 主管 Thread（父，supervisor）───────────────────────┐
│  state.messages[]        对话历史（子 agent 看不到）                          │
│  state.async_tasks{}     task_id → {agent_name, thread_id, run_id, status}   │
│  工具：start / check / update / cancel / list_async_task、write_todos        │
└───┬──────────────────────────────────────────────────────────▲─────────────┘
    │ ① 任务描述（消息，单向给出）                                │ ③ 结果投递（消息，单向送回）
    │  runs.create(thread_id=子线程,                              │  主管 run 开始 → before_agent 写一条消息
    │    input={messages:[{role:user, content:description}]})     │  主管已空闲 → 发件箱分发者开新一轮（custom/service/outbox_dispatcher.py）
    │  ❗只传这段 description，父对话历史一概不传                    │    "[Async subagent 'X' has completed] Result: …"
    │                                                            │  结果 = 子 agent 最后一条消息原文（不截断）
    │ ② config.configurable（元数据，不进消息）                   │  身份：以父线程属主的身份建 run（服务端按 user_space 复原权限）
    │  user_spaces = [ 本任务的那一个 space ]                      │
    │  ❗服务端注入的 user_spaces 收窄成单空间，越权由后端拒绝         │ ④ 拉取（工具调用，非推送）
    │                                                            │  check_async_task → runs.get → status + result
    │ 子线程 metadata（归属，另走 threads.create）                │  顺带把 state.async_tasks 改成真实状态
    │  owner_identity / parent_thread_id                         │  ❗parent_thread_id 是 finalize_run 写发件箱行的判据
    ▼                                                            │
┌─────────────────────── 子 Thread（cbam-reporting 等）──────────────────────┐
│  自己的 messages[]、自己的 checkpoint、自己的 space 可见范围                 │
│  收尾：子 run 转终态 → finalize_run 写一行发件箱（不在图里做任何事）          │
└──────────────────────────────┬────────────────────────────────────────────┘
                               │ ⑤ 实时旁路（前端直连，不经过父 agent）
                               ▼
         GET /custom/tasks/{task_id}/stream?parent_thread_id=…
         服务 token 订阅子 run 的 messages/custom → 转成 SSE 给浏览器
```

| # | 方向 | 载体 | 谁写 | 代码位置 | 失败时 |
|---|---|---|---|---|---|
| ① | 父 → 子 | 子线程第一条 `user` 消息 | 拦截层 | `graphs/middleware/async_task_guard.py::_start_sync/_astart` | 工具返回 `Failed to launch…` |
| ② | 父 → 子 | `config.configurable` | 拦截层 | 同上 `_config_for()` | 无权限时直接拒绝派活 |
| ③ | 子 → 父 | 父线程里一条消息（写 state 或新 run） | 主管侧 guard / 发件箱分发者 | `async_task_guard.py::abefore_agent`（run 开始）/ `custom/service/outbox_dispatcher.py`（主管空闲或 run 期间完成的） | 分发者会重试；仍失败时靠 `check_async_task` 兜底 |
| ④ | 子 → 父 | 工具返回值 + state 更新 | 主管主动调用 | `deepagents/…/async_subagents.py::_build_check_result` | 返回 `Failed to get run status` |
| ⑤ | 子 → 前端 | SSE 帧 | 代理接口 | `custom/api/::stream_task` | 发 `error` 帧后 `end` |

`update_async_task(task_id, message)` 是 ① 的复用：往**同一个**子线程追加一条 `user` 消息并开新 run（`multitask_strategy="interrupt"` 顶掉旧 run）。

### ③ 结果什么时候真正到达主管

主管**从不等待**后台任务：派完继续干自己的活，结果到达时再插进来。两条投递路径按主管
此刻的处境分工。注意：分发者判“主管在跑”只看 `runs` 表有没有 `pending` / `running`
行，**不看下面这个 `Thread.status`**（它只是给人看的）：

| 主管此刻 | 谁投递 | 行为 |
|---|---|---|
| run 刚开始 | 主管侧 guard（`abefore_agent`） | 把已终态的结果写进 state；**不打断**、不另开 run，本次 run 的模型调用就能看到 |
| run 期间才完成 | 分发者（run 结束后） | **不在该 run 里写**：那时模型这一轮已答完，写进去没法转述；run 结束后另开一轮唤醒 run |
| run 跑到一半需要结果 | 模型自己 | 调 `check_async_task`——它返回**全文**，而且 guard 会记下已投递，之后不再重复投 |
| `interrupted`（图在等 resume，例如正问用户问题） | 谁都不动 | 插一轮 run 会把线程从 `interrupted` 翻成 `busy`，用户点“回答”时会被 400 拒掉；等用户答完，那次 resume 的 hook 自动补上 |
| `idle` / `error`（主管已经答完） | 发件箱分发者（服务端） | 子 run 终态时 `finalize_run` 已写了一行发件箱；分发者把一个 tick 里同一父线程的待投结果合成**一轮**唤醒 run |

两条路不重复：发件箱行 `async_task_outbox` 的主键就是 `(task_id, run_id)`，投递前先用
`POST /custom/tasks/{id}/deliver-claim` 把行从 `pending` **原子翻成 `delivered`**，
翻不成就说明别人已投（单行条件 UPDATE，没有时间窗口、没有 advisory lock）。
**抢不到就直接不投**（包括请求超时、服务不可达）——行还在 `pending`，下一轮分发者或下一轮
run 会再试；只有“确实抢到”才投，所以同一个结果不会出两张卡片。
投过的任务在 state 里留 `delivered_run_id`（它的作用只是省掉下次的 claim，丢了能重建）。
判重只认这个字段，**不翻聊天记录找卡片**：摘要会把旧消息压成一段总结、模型自己也能复述
一段带 `task_id:` / `run_id:` 的文本，两种都会让判重说谎。
判重键含 `run_id`，因为 `update_async_task` 会给同一任务开新 run。

投递消息带子 agent 最后一条消息的**原文，不截断**（超过 3000 字记一条告警，但内容不删减）。
用表而不是读 state 判重，是因为跨进程才原子，而且不用为了查一个标记把整份对话读回来。主 prompt 另外要求模型对重复通知只更新 todo。

### 多个子任务同时完成会怎样

| 情形 | 结果 |
|---|---|
| run 开始时已有几个结果待投 | 它们在**一次** `before_agent` 里合并成一条 state 更新，各一条消息，不多跑一轮模型 |
| 几个任务在主管 run 期间收尾 | guard 不写 state；run 结束后分发者开一轮唤醒 run，模型这轮能主动转述 |
| 几个子 agent 几乎同时收尾，主管空闲 | 分发者一个 tick 领到全部 → 只开**一轮**唤醒 run，input 拼了所有结果（同一时刻最多一个投递 run，跨 worker 也成立） |

子 agent 自身不受影响：每个任务在自己的线程里独立执行，同时完成只是把结果一起排队送到主管。

投递的重试退避与领取租约是 `custom/service/outbox_dispatcher.py` 的模块常量，取值在根 `config.yaml` 的
`outbox.*`（也可以用同名环境变量覆盖），语义见 [adr/0010](./docs/adr/0010-task-delivery-idempotency.md)。

**仍然存在的空隙**：结果在主管 run 期间完成时不会插进那一轮，而要等 run 结束后另开一轮
唤醒 run（多花一轮 LLM，换来模型能对结果说一句话）；主管一直忙且用户不停说话时，
发件箱行会一直 `pending`（结果晚到，不丢）；卡片正文靠现查子线程获取，自调失败时退化成
`(no output)`——完整清单见 [adr/0010 §已知问题与残留风险](./docs/adr/0010-task-delivery-idempotency.md#已知问题与残留风险)。

### 明确**不**流通的东西

- **父对话历史**：子 agent 只拿到 `description`，看不到用户之前问了什么。所以派活时要把路径、问题、约束一次写清。
- **子 agent 的 token 流 / 工具事件**：不会回到父的 SSE。想在界面上看，只能走 ⑤ 的代理。
- **其它任务的信息**：子 agent 只知道自己的 space，`GET /custom/tasks` 也只放行本父线程下的 task。
- **权限的外溢**：② 里下发的是 `[本任务 space]` 而不是用户全部空间；子 agent 的 `SpaceBackend` 和 xlsx 工具都按这个列表判定。
- **记忆的延续**：子 agent 的 checkpoint 在自己的 thread 上；父线程只存 task 句柄，不存子 agent 的中间状态。

---

## 11. 新增一个子 agent（= 新增一个 Space）

**不用改代码**。一个 Space 就是一个子 agent：`space` 表一行 + 知识空间下一个文件夹 +
该文件夹里的 `AGENT.md`。名单、prompt、工具都是运行时按这些派生的（见
[adr/0011](docs/adr/0011-space-agent-is-dynamic.md)）。

### 11.1 建空间（推荐走接口）

```bash
curl -s -X POST http://127.0.0.1:2026/custom/spaces/create \
  -H "Authorization: Bearer <超管 token>" -H "Content-Type: application/json" \
  -d '{"space_name": "bio-lab"}'
# → {"space_name": "bio-lab", "message": "bio-lab 已经建立"}
```

接口会做三件事：名字校验（字母/数字/下划线/点/短横线，1–64 字符）、按知识空间根派生目录
（`knowledge/bio-lab`，请求里的 `path` 已废弃）、目录里没有 `AGENT.md` 时从模板生成一份。

手写也行，但**三样缺一不可**：

```bash
mkdir -p knowledge/bio-lab
psql -U postgres -d aegra <<'SQL'
insert into space(name, path) values ('bio-lab', 'knowledge/bio-lab') on conflict (name) do nothing;
-- 谁能用这个空间（不给就没任何人能派活给它）
insert into user_space(identity, space_name) values ('root', 'bio-lab') on conflict do nothing;
SQL
```

> 拦截层派活前会校验「当前用户的 spaces 包含该空间」，不包含就拒绝：
> `用户没有空间 'bio-lab' 的权限，无法启动该子 agent`。

### 11.2 写 `AGENT.md`（唯一的 prompt 来源）

`knowledge/bio-lab/AGENT.md`：frontmatter 的 `description` 是**主管选人用的描述**
（比正文更关键），正文是**子 agent 自己的 system prompt**。

```markdown
---
description: 生物实验室领域专家，运行在 bio-lab 知识空间。负责菌株保藏/复苏、培养基配方、实验记录查询。用户问题涉及菌株、培养基、传代、生物实验 SOP 时派给我。
---

你是生物实验室领域的专用知识 agent，只读不写。主 agent 转述的任务会带 /bio-lab/xxx 路径。

## 知识库（先读后答）
- /bio-lab/index.md：总览与导航
- /bio-lab/strain-bank.md：菌株保藏与复苏边界

## 流程
最终答案 = 结论 + 出处，不要贴超长原表。
```

两处兜底：没有 `AGENT.md` 时 prompt 退到一句通用说明、description 退到空间名；有正文但没写
`description` 时用正文首行。

### 11.3 放知识（磁盘）

```
knowledge/bio-lab/
├── AGENT.md          # 上面那份：description + system prompt
├── index.md          # 必备：OKF v0.2（frontmatter `okf_version: "0.2"` + 分组链接）
└── xxx.md / *.xlsx   # 概念文档与表格
```

**有 `.xlsx` 才会装 xlsx 四件套**（`xlsx_info/query/search/profile` + CodeInterpreterMiddleware），
纯文档空间自动不装。表在**第一次查询时**解析成 Parquet（`xlsx.cache_dir`），之后走 DuckDB。

> 空间知识也可以放对象存储（多副本读同一份，`config.yaml` 的 `okf.backend`）：
> `s3` = 每个副本拉一份本地镜像（改完跑 `scripts/okf_push_s3.py`，各副本跑
> `scripts/okf_pull_s3.py`）；`s3fs` = Mirage 直读、不拉镜像。全部配置见
> [docs/S3.md](./docs/S3.md)；两种模式与单机模式的对照见 [docs/S3.md §0.1](./docs/S3.md)。

### 11.4 验证（不用重启）

```bash
# 空间能被解析、AGENT.md 读得到
PYTHONPATH=. uv run python -c "
from shared.knowledge.spec import load_agent_spec, has_tables
d, p = load_agent_spec('bio-lab'); print(has_tables('bio-lab'), d[:40], len(p))"
```

名单是**每次 run 现算**的，所以下一次对话就能派给它；正在跑的 run 看不到新空间。
`/custom/graphs` 里永远只有三个图：`supervisor`、`domain_agent`、`xlsx_agent`——空间不再是一个 graph key。

### 11.10 不用手写的东西

拦截层 + 单个通用图已经把这些固定好了，新增空间不用重复实现：

| 能力 | 来自 |
|---|---|
| 只能读本 space 的文件（含 ls/grep/glob 过滤） | `graphs/access/backend.py::SpaceBackend` |
| 写操作全禁（纵深防御） | `FilesystemPermission(write=deny)` + backend |
| 完成/出错时把结果送回父线程 | `AsyncTaskGuard`（主管 run 首尾写 state）+ `custom/service/outbox_dispatcher.py`（主管空闲时开一轮唤醒 run） |
| 任务级权限收窄（只下发本 space） | `graphs/middleware/async_task_guard.py::_config_for` |
| 一个账号最多几个会话（`quota.max_threads_per_user`，默认 50，含子线程）+ 僵尸状态核实 | `custom/middleware/auth.py::_quota_threads_create` + `AsyncTaskGuard._refreshed_*` |
| 前端能实时看这个子 agent 的输出 | `runs.create(stream_mode=["messages","custom"])` |
| prompt 与描述 | `<space>/AGENT.md`（`shared/knowledge/spec.py`） |

### 11.11 常见坑

| 现象 | 原因 |
|---|---|
| 主管永远不派给新 agent | 没给这个用户分配 space，或 `AGENT.md` 的 `description` 写得太宽/太窄 |
| `Unknown async subagent type` | `space` 表没登记，或表里有但目录 `knowledge/<space>/` 不存在（会被跳过并记日志） |
| `用户没有空间 'bio-lab' 的权限` | `user_space` 表没登记（§11.1） |
| 子 agent 说「无权访问 /bio-lab/...」 | 磁盘目录不存在，或 `space.path` 与实际目录不一致（由接口建的不会错） |
| 子 agent 读不到 xlsx 工具 | 空间里没有 `.xlsx`（按文件自动决定） |
| 刚刚新建的空间这个对话里用不了 | 名单只在 run 开始时确定，下一次对话才可见 |
| 改了 `AGENT.md` 没生效 | prompt 在建图时读，新 run 生效；当前 run 不会变 |

---

更多接口与流式细节见 [`docs/frontend-api.md`](docs/frontend-api.md)、[`docs/streaming.md`](docs/streaming.md)；
术语见 [`CONTEXT.md`](CONTEXT.md)。
