# aegra-api 配置

aegra-api 的配置分三份文件，职责不重叠：

| | `aegra.json` | `config.yaml` | `.env` / 环境变量 |
|---|---|---|---|
| **放什么** | 这个部署**对外提供什么**：图、自定义 HTTP app、store / checkpointer / auth 策略 | 本项目新增的**非密钥**参数：会话配额、文件上限、对象存储地址、OKF、发件箱 | aegra 原生运行参数（host / port、PostgreSQL、Redis、worker、cron、日志、OTEL）+ **密钥** + 第三方 SDK |
| **谁读** | `src/aegra_api/config.py` | `shared/config.py` | `src/aegra_api/settings.py`（`load_dotenv()` 在导入时执行）+ 各模块 `os.environ` |
| **格式** | JSON | YAML | 项目根 `.env`，`KEY=VALUE` |
| **放密钥** | 不放 | **不放** | 放 |
| **优先级** | — | 默认值 < 文件 < 同名环境变量 | 真实环境变量 > `.env` |

判断标准：

- 描述“这个服务托管什么”（托管哪些图、用哪个 auth 处理器、保留策略）→ `aegra.json`
- 本项目自己加的调参（配额、上限、S3 地址、OKF、发件箱）→ `config.yaml`
- 跟机器/部署绑定的 aegra 原生参数、以及任何凭据 → `.env`

三者的逐键说明见 [§3](#3-配置来源)。

---

## 1. 配置文件在哪、什么时候生效

### 解析顺序

1. 环境变量 `AEGRA_CONFIG`（相对**进程 CWD**，文件存在才采用）
2. `./aegra.json`
3. `./langgraph.json`（兼容 LangGraph CLI 的旧命名）

三个都没有 → **启动失败**：

```
ValueError: Configuration file not found. Expected one of: AEGRA_CONFIG path, ./aegra.json, or ./langgraph.json
```

JSON 语法错误则直接抛 `json.JSONDecodeError`，没有包装。两者都发生在 FastAPI lifespan 里，所以是"起不来"，不是"跑起来后某个接口报错"。

### 什么时候生效

**只在启动时读一次**（`LangGraphService.initialize()`，由 `main.py` 的 lifespan 调用）。改完 `aegra.json` 必须重启进程；`graphs` 里的每个模块在启动时都会被 eager import，模块里的顶层代码（建图、连外部服务）也会在那时执行。

### 相对路径的基准

| 配置项 | 相对谁 |
|---|---|
| `graphs` 的图文件路径 | 配置文件所在目录 |
| `dependencies` | 配置文件所在目录 |
| `http.app` | 配置文件所在目录（否则按点号模块导入） |
| `auth.path` | 配置文件所在目录（拿不到就用 CWD） |
| `.env` 里的 `LOG_FILE` 等 | 进程 CWD（和环境变量语义保持一致） |

### 用 `AEGRA_CONFIG` 切两套配置

仓库自带 [`aegra_example.json`](../aegra_example.json)：完整示例（`dependencies` + `examples/` 下的 8 个图 + `http.app` + `store.scopes`）。跑单元/集成测试和跑单个 demo 需要不同的图集合，用环境变量切换，别来回改同一个文件：

```bash
# 只跑 demo（最小配置）
uv run python src/aegra_api/main.py

# 需要完整图集合（e2e / examples）
AEGRA_CONFIG=aegra_example.json uv run python src/aegra_api/main.py
```

---

## 2. `aegra.json` 逐键说明

### `graphs`

唯一必须存在的键（缺了服务能起来，但没有任何图可跑）。

```json
{
  "graphs": {
    "supervisor": "./graphs/supervisor.py:graph",
    "domain_agent": "./graphs/space_agent.py:graph"
  }
}
```

> 上面是本仓库的真实配置。`graphs` 的**键名会写进 DB**（`assistant.graph_id`），改名等于让历史
> assistant 指向不存在的图；要调整布局只改冒号后面的文件路径。

- 值必须是 `"<文件路径>:<导出名>"`。**只接受文件路径**，不支持 `mypackage.graph:graph` 这类点号模块导入（会被当成相对文件路径 `mypackage/graph`，然后报 `Graph file not found`）。
- `./` 前缀可有可无；路径相对配置文件目录。
- 导出必须是一个已编译的图（`StateGraph` / `Pregel` 实例），或者是工厂函数。**不要自己传 checkpointer**，服务端按请求注入。
- 工厂函数：0 参数 → 启动时调用一次；1～2 参数且带类型注解（`ServerRuntime` 等）→ 每次请求用真实的 runtime/config 调用。3 个以上参数会在启动时报错。
- 启动时会 import 全部图模块，任一模块导入失败都会让启动失败。

`graph_id` → 模块名的清洗规则：`.`、`/`、`-` 一律替换成 `_`，挂到 `aegra_graphs.` 命名空间下。因此这些 key 会**在启动时冲突并报错**：

```json
"graphs": { "a.b": "...", "a_b": "..." }
```

```
ValueError: Graph IDs 'a.b' and 'a_b' collide after sanitisation (both map to aegra_graphs.a_b)
```

没有保留 key，没有数量上限，key 也没有字符集校验。

### graph key 就是 assistant 身份（最容易踩的一块）

一个 key 同时是三样东西：

1. `aegra.json` 里的 key
2. 数据库里 assistant 记录的 `graph_id`
3. 客户端可以直接当 `assistant_id` 用的名字

启动时，服务会为每个 key 预建一条 assistant（幂等）：`assistant_id = uuid5(6ba7b821-9dad-11d1-80b4-00c04fd430c8, key)`，`name = key`，`user_id = "system"`。客户端传 `assistant_id` 时的解析规则（`src/aegra_api/utils/assistants.py`）：

| 传进来的值 | 解析结果 |
|---|---|
| 当前配置里的某个 graph key | 映射到上面那条 uuid5 记录 |
| 不是 graph key | 当成字面量 assistant_id 去查库；查不到 → 404 |

所以**从 `aegra.json` 删掉一个 key 等于把那个 assistant 的身份抽走**：任何指向它的客户端（例如 Deep Agents 的 `AsyncSubAgent(graph_id=...)`）下次调用立刻失败。两种报错含义不同：

| 报错 | 含义 |
|---|---|
| `Assistant 'researcher' not found` | 这个 id 既不是库里的 assistant，也不是配置里的 graph key（key 被删或名字写错） |
| `Graph 'old_name' not found for assistant` | 库里那条 assistant 记录还在（过去建过），但它引用的 graph key 已从配置里消失 |

删除的 key 对应的 assistant 记录**不会被清理**，会一直留着。

### `dependencies`

额外加进 `sys.path` 的目录，用于图模块要导入项目根之外的共享代码：

```json
{ "dependencies": ["./examples"] }
```

相对配置文件目录（绝对路径原样使用）。**先写的优先级更高**（内部倒序插入到 `sys.path[0]`）。目录不存在只打 warning，不致命——但如果图模块依赖它，导入失败会让启动失败。

### `http`

```json
{
  "http": {
    "app": "./examples/custom_routes_example.py:app",
    "enable_custom_route_auth": true,
    "cors": { "allow_origins": ["https://app.example.com"] }
  }
}
```

| 键 | 说明 |
|---|---|
| `app` | 自定义 FastAPI app 的导入路径：`./file.py:app` 或 `mypackage.routes:app`。**是替换而不是挂载**——你的 app 会带上内置路由（`/threads`、`/runs` …）一起对外，`create_app` 会用你的对象当应用本体。必须是 `FastAPI` 实例，否则报 `Custom apps must be FastAPI applications. Use: from fastapi import FastAPI; app = FastAPI()` |
| `enable_custom_route_auth` | 给自定义 app 的路由套上 Aegra 的鉴权依赖；覆盖不到的（纯 Starlette 路由 / mount）会打 warning |
| `cors` | 键：`allow_origins`、`allow_origin_regex`、`allow_credentials`、`allow_methods`、`allow_headers`、`expose_headers`、`max_age`。默认 `allow_origins=["*"]`、`allow_methods=["*"]`、`allow_headers=["*"]`、`max_age=600`；配了 `allow_origin_regex` 时 `allow_origins` 默认变空数组，`allow_credentials` 只有在显式写了具体 origin（非 `*`）时才默认 true |

不配 `http` 也能跑，只是没有自定义路由。

### `store`

```json
{
  "store": {
    "scopes": { "orgs": ["org_id"] },
    "index": { "dims": 1536, "embed": "openai:text-embedding-3-small", "fields": ["$"] }
  }
}
```

| 键 | 说明 |
|---|---|
| `scopes` | "命名空间前缀 → 允许用来做 scope 的用户属性"。默认所有 store 数据都挂在 `["users", <你的身份>]` 下；配了 `{"orgs": ["org_id"]}` 后，以 `orgs` 开头的命名空间改成挂在 `["orgs", <该用户的 org_id>]` 下，于是**同一个属性的用户共享数据** |
| `index` | 打开 pgvector 语义搜索：`dims`（向量维度）、`embed`（`provider:model`）、`fields`（要嵌入的 JSON 路径，默认 `["$"]`）。aegra 不做校验，原样传给 LangGraph 的 `AsyncPostgresStore` |

写错 `scopes` **不会报错**，只会打 warning 然后忽略该条（前缀 `users` 是保留的，配了也会被忽略）。用户缺少配置里声明的属性时，请求返回 403：`User has no 'org_id' required for 'orgs'-scoped store access`。

### `checkpointer`

```json
{
  "checkpointer": {
    "durability": "async",
    "ttl": { "strategy": "delete", "default_ttl": 10080, "sweep_interval_minutes": 30 }
  }
}
```

| 键 | 取值 | 说明 |
|---|---|---|
| `durability` | `sync` / `async` / `exit` | 没显式指定 durability 的 run 用它。非法值启动时报错 |
| `ttl.strategy` | `delete` / `keep_latest` | 过期线程是整条删（默认）还是只留最新 checkpoint |
| `ttl.default_ttl` | 分钟，默认 43200（30 天），需 > 0 | 线程保留时长 |
| `ttl.sweep_interval_minutes` | 默认 5，需 > 0 且 ≤ 上限 | 清理任务扫描间隔 |
| `ttl.sweep_limit` | 默认 10000，需 > 0 | 单轮最多清理多少条 |

环境变量 `AEGRA_THREAD_TTL` 会**覆盖** `checkpointer.ttl`（可以写数字 = 分钟数，也可以写完整 JSON），`AEGRA_CHECKPOINT_DURABILITY` 覆盖 `checkpointer.durability`；单个 run 上传的值优先级最高。

### `auth`

```json
{ "auth": { "path": "./auth.py:auth" } }
```

`path` 支持 `./auth.py:auth`（相对配置文件目录）和 `mypackage.auth:auth`（已安装包）。不配 = 匿名身份，`AUTH_TYPE=noop`。

**`disable_studio_auth` 是一个死键**：它在 `AuthConfig` 里声明、docstring 写的是 "Disable authentication for LangGraph Studio connections"，但**全仓库没有任何代码读它**——填 `true` 或 `false` 对行为都没有影响。保留声明只是为了不破坏与 LangGraph auth 配置同形的写法；不要指望它关掉任何鉴权。

### 不支持的东西

- `aegra.json` 里**没有** `${VAR}` 环境变量插值，JSON 原样加载。要按环境切换就换文件（`AEGRA_CONFIG`）或走环境变量。
- graph 值不支持点号模块导入（见 `graphs`）。
- 没有为 `graphs` / `http` / `store` 之外预留的隐藏键。

---

## 3. 配置来源

三份文件、三套职责，互不覆盖：

| 来源 | 放什么 | 谁读 | 优先级 |
|---|---|---|---|
| `aegra.json` | aegra 原生：托管哪些图、自定义 HTTP app、store / checkpointer / auth（见 §2） | `src/aegra_api/config.py` | — |
| `config.yaml`（仓库根） | 本项目新增的**非密钥**参数：配额、会话文件上限、对象存储地址、知识空间、发件箱 | `shared/config.py` | 代码默认值 < 文件 < 同名环境变量 |
| `.env` / 环境变量 | aegra 原生 env（§3.1）、**密钥**（§3.3）、第三方 SDK（§3.4） | `settings.py` 与各模块 `os.environ` | 环境变量总优先 |

一个键该放哪：

- 影响 aegra 本身怎么跑（host / port / PG / Redis / 日志 / OTEL）→ `.env`（§3.1，上游定义）
- 本项目加的调参（配额、上限、S3 地址、OKF、发件箱）→ `config.yaml`（§3.2）
- 凭据（JWT 密钥、密码 pepper、S3 凭证、API key）→ `.env`（§3.3）

### 3.1 aegra 原生环境变量（`src/aegra_api/settings.py`）

升级 aegra 时这段原样跟着上游走，不要往里塞业务配置。未声明的键被静默忽略；`.env` 在导入时加载一次，**真实环境变量优先**，所以同一份 `.env` 在 Docker/K8s 里可以只覆盖几个键。

#### 服务与运行

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `2026` | 监听地址；`SERVER_URL` 在未显式设置时由这两者推导 |
| `AEGRA_CONFIG` | `aegra.json` | 上文那份配置文件的路径（相对 CWD） |
| `AUTH_TYPE` | `noop` | `noop` / `custom` |
| `ENV_MODE` | `LOCAL` | `LOCAL`/`DEVELOPMENT` → 控制台彩色输出；其他值 → JSON。同时作为 OTEL 的 `deployment.environment` |
| `DEBUG` | `false` | 只影响 `FastAPI(debug=...)`，**不是**日志级别开关 |
| `MAX_SEARCH_LIMIT` | `1000` | 搜索类接口的 limit 上限 |
| `RUN_MIGRATIONS_ON_STARTUP` | `true` | 多副本部署设 `false`，改用 `aegra db upgrade` 单独执行 |
| `KEEPALIVE_INTERVAL_SECS` | `5` | join/wait 端点的心跳间隔 |
| `FF_V2_EVENT_STREAMING` | `true` | 新版事件流开关 |

#### PostgreSQL 与连接池

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | 无 | 设了就**优先于**下面五项 |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_DB` | `postgres` / `postgres` / `localhost` / `5432` / `aegra` | 分项配置 |
| `DB_ECHO_LOG` | `false` | SQLAlchemy 回显 SQL（走日志管线，量很大） |
| `SQLALCHEMY_POOL_SIZE` / `SQLALCHEMY_MAX_OVERFLOW` | `10` / `20` | API 侧连接池 |
| `LANGGRAPH_MIN_POOL_SIZE` / `LANGGRAPH_MAX_POOL_SIZE` | `5` / `20` | 图执行侧连接池 |

#### Redis 与 worker

| 变量 | 默认 | 说明 |
|---|---|---|
| `REDIS_BROKER_ENABLED` | `false` | `false` = 进程内 asyncio 执行器 + 内存 broker；`true` = Redis 作业队列，具备崩溃恢复与多副本 |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `REDIS_CHANNEL_PREFIX` / `REDIS_MAX_CONNECTIONS` / `REDIS_HEALTH_CHECK_INTERVAL` / `REDIS_RETRY_ATTEMPTS` | `aegra:run:` / `250` / `30` / `3` | |
| `WORKER_COUNT` / `N_JOBS_PER_WORKER` | `3` / `10` | 仅 `REDIS_BROKER_ENABLED=true` 时生效 |
| `BG_JOB_TIMEOUT_SECS` / `BG_JOB_MAX_RETRIES` | `3600` / `3` | 后台作业超时与重试 |
| `LEASE_DURATION_SECONDS` / `HEARTBEAT_INTERVAL_SECONDS` / `REAPER_INTERVAL_SECONDS` | `30` / `10` / `15` | 租约与僵尸回收 |
| `STUCK_PENDING_THRESHOLD_SECONDS` / `POSTGRES_POLL_INTERVAL_SECONDS` | `120` / `5` | 卡住判定与回退轮询 |
| `WORKER_QUEUE_KEY` / `WORKER_DRAIN_TIMEOUT` | `aegra:jobs` / `30` | 队列键名与优雅停机时长 |

**多副本 / worker 模式必须开 broker**（原 [ADR-0009](./adr/archive/0009-worker-mode-requires-redis-broker.md)）：
`REDIS_BROKER_ENABLED=false` 时 run 只活在进程内存里，取消请求落到别的进程就静默失效——实测 2 个
uvicorn worker 下**约 50% 的取消丢失**（DB 已写 `interrupted`，属主进程里的图继续跑、继续写
checkpoint），而启动日志里**没有任何告警**。

- 开了 broker：run 进 Redis List，带 `claimed_by` + `lease_expires_at`，取消走 pub/sub 找到属主进程；
  事件也进 Redis，任一实例都能 attach 并重放，SSE 不再依赖"连到属主进程"。
- 单机开发想省 Redis：`REDIS_BROKER_ENABLED=false` + **只用单进程**启动即可；但不要用 `uvicorn --workers N`。
- 代价是 at-least-once 重投：进程崩溃后 lease 过期会被重新执行，幂等由业务方负责。

#### Cron

| 变量 | 默认 | 说明 |
|---|---|---|
| `CRON_ENABLED` | `true` | 关掉则不跑 cron 调度 |
| `CRON_POLL_INTERVAL_SECONDS` / `CRON_CLAIM_DURATION_SECONDS` | `60` / `300` | 扫描与认领 |
| `CRON_MAX_PER_USER` / `CRON_TICK_BATCH_SIZE` / `CRON_MAX_PAYLOAD_BYTES` | `100` / `100` / `65536` | 配额与批量 |
| `CRON_ALLOW_SECONDS_SCHEDULE` | `false` | 是否允许秒级调度 |

#### 保留策略

| 变量 | 默认 | 说明 |
|---|---|---|
| `AEGRA_THREAD_TTL` | 无 | 覆盖 `checkpointer.ttl`；可写分钟数，也可写 `{"strategy":"keep_latest","default_ttl":...}`。别名 `LANGGRAPH_THREAD_TTL` |
| `AEGRA_CHECKPOINT_DURABILITY` | 无 | 覆盖 `checkpointer.durability` |

### 3.2 本项目新增：`config.yaml`（`shared/config.py`）

非密钥的项目参数全在这一个文件里。密钥不要往里写（§3.3）。

- **位置**：仓库根 `config.yaml`（`shared/config.py` 按自身位置找，与 CWD 无关）。
- **生效时机**：import 时读一次 → **改完要重启进程**（与 `aegra.json` 一致）。
- **优先级**：同名环境变量 > 文件 > 代码内默认值。留口子给容器 `-e` 和单测 `monkeypatch.setenv`。
- **写错不将就**：YAML 语法错、键拼错、值类型错（该写数字写成了文字）都会在启动时抛 `ValueError`，不会静默退回默认值。
- **没写 = 用默认值**：文件里可以只写要改的键；整份删掉也能跑（全用默认）。

下表「覆盖变量」列的用途只有两个：容器里临时改一个键、单测里打桩。日常改配置改 YAML。

#### 配额（`custom/service/quota.py`）

| YAML 键 | 默认 | 覆盖变量 | 说明 |
|---|---|---|---|
| `quota.max_threads_per_user` | `50` | `MAX_THREADS_PER_USER` | 一个账号最多同时拥有几个会话（主管线程 + 子线程；子线程计到所属主管上）。超额时 `POST /threads` 返回 403。超管不受限。见 [adr/0012](./adr/0012-thread-quota-replaces-task-limit.md) |

#### 身份与 token（`custom/service/account.py`、`custom/middleware/auth.py`、`graphs/access/spaces.py`）

| YAML 键 | 默认 | 覆盖变量 | 说明 |
|---|---|---|---|
| `auth.service_identity` | `subagent_service` | `SERVICE_IDENTITY` | 服务身份名：子 agent 线程的属主、`custom/middleware/auth.py` 免查库放行、`custom/api/delivery.py` 跨用户放行都按它判断。常量定义在 `shared/token.py`，各调用点都从那里取 |
| `auth.access_token_ttl` | `3600` | `ACCESS_TOKEN_TTL` | **用户** access token 的有效期（秒）。只影响 `/register`、`/login` 签发的 token；子 agent 的服务 token 不受此值影响（见下方说明） |

> **两种 token 别混**（都在 `shared/token.py`）：
>
> - `create_access_token(identity)` —— **用户的**。`sub` = 具体账号，`exp = iat + auth.access_token_ttl`，默认 1 小时；过期后请求 401，前端回登录页。没有 refresh token，所以这个值就是“用户多久重新登录一次”。
> - `service_token()` —— **子 agent 的**。`sub` = `auth.service_identity`（默认 `subagent_service`），`exp` **写死 10 年**，进程内缓存；供主管派活（`graphs/supervisor.py`）、前端代理读子线程、取消子任务、发件箱分发使用。
>
> 所以：改 `access_token_ttl` 只会变用户的登录频率，子 agent 照常跑；反过来想“让服务 token 失效”只能改 `JWT_SECRET`（代价：所有用户 token 同时失效）。

#### 会话文件 md（`shared/session/store.py`）

md 会话文件的 Store 载体。**main 已移除上传入口**，这一组键只剩底座在模块导入时读取（保留给后续分支）。

| YAML 键 | 默认 | 覆盖变量 | 说明 |
|---|---|---|---|
| `session_files.max_upload_mb` | `10` | `MAX_UPLOAD_MB` | md 单文件上限（当前无入口） |
| `session_files.max_files_per_session` | `50` | `MAX_FILES_PER_SESSION` | md 每会话文件数 |
| `session_files.upload_ttl_minutes` | `4320`（72h） | `UPLOAD_TTL_MINUTES` | md 的孤儿保留期，语义是「最后读取后多久」 |

#### xlsx 工具（`graphs/tools/xlsx/`）

把知识空间里的存量 xlsx 建 SQLite 索引再查（`xlsx_info/query/search/profile`）。上传相关的三个键（`max_blob_mb`、`max_xlsx_files_per_session`、`max_blob_mb_per_user`）现在**无人读**，仅为兼容保留。

| YAML 键 | 默认 | 覆盖变量 | 说明 |
|---|---|---|---|
| `xlsx.max_blob_mb` | `10` | `MAX_BLOB_MB` | xlsx 单文件上限（当前无入口） |
| `xlsx.max_cells` | `2000000` | `MAX_CELLS` | 表结构硬闸；超了第一次调工具就报错，不静默截断 |
| `xlsx.max_xlsx_files_per_session` | `20` | `MAX_XLSX_FILES_PER_SESSION` | 每会话 xlsx 个数（当前无入口） |
| `xlsx.max_md_files_per_session` | `10` | `MAX_MD_FILES_PER_SESSION` | ⚠ **当前代码没读这个键**，改它不生效 |
| `xlsx.max_blob_mb_per_user` | `2048` | `MAX_BLOB_MB_PER_USER` | 每用户二进制总量（当前无入口） |
| `xlsx.cache_dir` | `.cache/xlsx` | `XLSX_CACHE_DIR` | 索引的本机缓存（必须在 OKF 根之外） |
| `xlsx.query_budget_ms` | `5000` | `XLSX_QUERY_BUDGET_MS` | 单次 `where` 查询的 CPU 预算，超了 `interrupt()` |
| `xlsx.sql_policy` | `strict` | `XLSX_SQL_POLICY` | `where` 谓词校验：`strict` 走语法树白名单（默认）/ `free` 只拦多条语句与注释（s3 模式下能读到凭证可达的任意对象） |
| `xlsx.cache_mode` | `local` | `XLSX_CACHE_MODE` | 派生缓存介质：`local` 只落本机 / `s3` 同时写对象存储供多实例共享（未配对象存储时自动退回 `local`） |
| `xlsx.cache_prefix` | `xlsx-derived` | `XLSX_CACHE_PREFIX` | `cache_mode=s3` 时的对象前缀；派生对象按内容哈希命名，可随时清 |
| `xlsx.cache_local_max_mb` | `2048` | `XLSX_CACHE_LOCAL_MAX_MB` | 本机派生缓存预算，超了按最久未改动淘汰 |

#### 对象存储（`shared/session/blobs.py`）

| YAML 键 | 默认 | 覆盖变量 | 说明 |
|---|---|---|---|
| `s3.endpoint` | 空 | `S3_ENDPOINT` | 空 = 对象存储未启用；`okf.backend: s3` 只能改成 `local` |
| `s3.bucket` | `aegra-blobs` | `S3_BUCKET` | 桶名，启动时自动创建（幂等） |
| `s3.region` | `us-east-1` | `S3_REGION` | 任意值（SeaweedFS 不校验） |

#### 空间知识 OKF（`shared/knowledge/root.py`、`shared/knowledge/spec.py`）

见 [adr/0016](./adr/0016-spaces-in-object-storage.md)、[S3.md](./S3.md)。

| YAML 键 | 默认 | 覆盖变量 | 说明 |
|---|---|---|---|
| `okf.backend` | `local` | `OKF_BACKEND` | `local` 直接读 `graphs/<okf.dir>`；`s3` 读本地镜像 |
| `okf.dir` | `okf` | `OKF_DIR` | 空间根目录名（相对 `graphs/`） |
| `okf.mirror_dir` | `.cache/okf` | `OKF_MIRROR_DIR` | `s3` 模式的镜像目录 |
| `okf.mirror_ttl_seconds` | `0` | `OKF_MIRROR_TTL_SECONDS` | 镜像过期重拉的间隔；`0` = 只拉一次（改内容后手动跑 `scripts/okf_pull_s3.py`） |

#### 图（`graphs/supervisor.py`）

| YAML 键 | 默认 | 覆盖变量 | 说明 |
|---|---|---|---|
| `agents.server_url` | `http://127.0.0.1:2026` | `AEGRA_SERVER_URL` | supervisor 给 `AsyncSubAgent` 用的服务地址；容器里要填服务自身可达地址（如 `http://aegra-api:2026`） |

#### 任务结果发件箱（`custom/service/outbox_dispatcher.py`）

进程内后台循环，每个进程一个，靠 `FOR UPDATE SKIP LOCKED` + 租约分工。
**不依赖 Redis**（Redis 只用于 `executor.submit`）；不配置也能跑。语义见
[adr/0010](./adr/0010-task-delivery-idempotency.md)。

| YAML 键 | 默认 | 覆盖变量 | 说明 |
|---|---|---|---|
| `outbox.poll_interval_seconds` | `1.0` | `OUTBOX_POLL_INTERVAL_SECONDS` | 轮询间隔；空表时每 tick 一次索引扫描 |
| `outbox.batch_size` | `50` | `OUTBOX_BATCH_SIZE` | 一次 tick 最多领多少行 |
| `outbox.lease_seconds` | `30` | `OUTBOX_LEASE_SECONDS` | 领取租约；进程死了过期后别人接手 |
| `outbox.busy_backoff_seconds` | `2` | `OUTBOX_BUSY_BACKOFF_SECONDS` | 父线程有活跃 run 时的重看间隔（不计失败次数） |
| `outbox.max_attempts` | `10` | `OUTBOX_MAX_ATTEMPTS` | 建唤醒 run 连续失败多少次后标 `failed`，交由父 run 的 hook 兜底 |
| `outbox.startup_delay_seconds` | `5` | `OUTBOX_STARTUP_DELAY_SECONDS` | 启动后首次 tick 的延迟（等 DB/LangGraph 初始化完） |
| `outbox.summary_timeout_seconds` | `5` | `OUTBOX_SUMMARY_TIMEOUT_SECONDS` | 现查子线程摘要的超时；超时退到发件箱里的快照 |
| `outbox.internal_url` | 空 | `AEGRA_INTERNAL_URL` | 分发者读子线程用的自调地址；空 = `http://127.0.0.1:{PORT}`（IPv4 回环，`localhost` 在 Windows 下会先解成 `::1`） |

### 3.3 密钥：只走 `.env`

| 变量 | 默认 | 说明 |
|---|---|---|
| `JWT_SECRET` | **无（必填）** | HS256 签/验 access token。缺失 = 启动即 `KeyError: 'JWT_SECRET'` |
| `PASSWORD_PEPPER` | **无（必填）** | 密码哈希前拼接的全局密文。改了这个值，已注册账号全部登不上（旧哈希不再匹配） |
| `SESSION_SCOPE_SECRET` | `dev-session-scope` | ⚠ **当前代码已不读**：会话作用域随上传功能一起移除，仅历史部署保留此键 |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | `dev` / `dev` | 对象存储凭证（SeaweedFS 未配 IAM 时任意值） |

### 3.4 第三方 SDK 与模型

| 变量 | 默认 | 谁读 |
|---|---|---|
| `OPENAI_API_KEY` | 空 | `graphs/model.py` 显式传给 `ChatOpenAI`（langchain-openai 也能自己从 env 取） |
| `OPENAI_BASE_URL` | 空 | 同上；OpenAI 兼容端点（DeepSeek / Moonshot / vLLM …）才需要 |
| `OPENAI_MODEL` | 空 | 同上，**没有 SDK 约定**：图代码自己取。留空时 `ChatOpenAI(model="")` 会在首次调用时报错 |

---

## 4. 日志

### 打到哪

**stdout 和控制台 + 文件**，两个 handler 同时挂（文件日志默认开启）：

| 出口 | 内容 |
|---|---|
| stdout | 人和容器采集器都看的一份 |
| `logs/aegra.log`（默认） | 始终 JSON，可直接喂采集器 |

全应用只有这两个 handler（`src/aegra_api/utils/setup_logging.py`），structlog 负责格式化，uvicorn 的 logger 被并入同一管线。

### 文件日志

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOG_FILE` | `logs/aegra.log` | 文件路径；相对路径相对**进程 CWD**。设成空字符串即关闭文件日志（只留 stdout） |
| `LOG_FILE_MAX_MB` | `10` | 单个文件上限，写满即轮转 |
| `LOG_FILE_RETENTION_DAYS` | `7` | 轮转出来的旧文件保留天数，超过的在下次轮转时删除 |

行为细节：

- 文件**永远是 JSON**（不受 `ENV_MODE` 影响），每行一个对象，含 `event`、`level`、`logger`、`timestamp`（ISO-8601）、`filename` / `func_name` / `lineno`，请求内还带 `request_id` / `http` / `network` / `duration`。
- 轮转文件命名为 `aegra.log.1`、`aegra.log.2` …（数字越大越旧）。
- **轮转和清理都发生在"写满那一刻"**：文件没写满就不会轮转，也就不会触发年龄清理。日志量很小的部署，旧文件可能一直留着；真在意磁盘占用就配外部 logrotate。
- stdlib 的规定：**空文件不轮转**，所以第一个文件至少要写进一条记录、且写满上限才会轮转。
- 目标目录不存在会自动创建；创建失败（例如只读工作目录）只打一条 warning 并**关闭文件日志**，服务照常启动，不会因为日志而起不来。
- 文件用 UTF-8 写，中文、emoji 都不会出问题。

### 日志级别与其它的开关

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOG_LEVEL` | `INFO` | 全局下限；同时作用于控制台和文件。想只看错误就设 `ERROR` |
| `LOG_VERBOSITY` | `verbose` | 只有 `verbose` 会在访问日志里带上 `request_id`；改成别的值就等于去掉它 |
| `LOG_EXCLUDE_PATHS` | 空 | 逗号分隔的路径前缀，命中且**状态码 < 400** 时不记访问日志（错误始终记录） |

### 关掉访问日志

没有专门的开关。想减少噪音只能：

- `LOG_LEVEL=ERROR`（会一起压掉其他 INFO）
- `LOG_EXCLUDE_PATHS=/health,/ready`
- 换入口命令（见下）

### 两个入口命令的差异

```bash
# 应用自己的日志配置生效，uvicorn 的访问日志并入同一管线
uv run uvicorn aegra_api.main:app --port 2026 --loop "asyncio:SelectorEventLoop"

# uvicorn.run() 会在导入之后重新装回它自己的默认 logging 配置，
# 于是会多出一份不走本管线的 uvicorn 访问日志（默认 INFO）
uv run python src/aegra_api/main.py
```

功能上都可用，只是 `python src/aegra_api/main.py` 这条多一份原样访问日志。

### 关联 ID

`CorrelationIdMiddleware` 读取请求头 `X-Request-ID`（合法 UUID4 才采用，否则生成 `uuid4().hex`），并回写到响应头；同一个请求内的所有日志行都带 `request_id` 字段。

### 把日志留存到磁盘（进程外）

服务自己只做上面那套文件日志；要长期归档、集中采集时，按部署方式选择：

```bash
# 1) 本地：重定向 + logrotate（应用自身文件日志可以关掉）
LOG_FILE= nohup uv run python src/aegra_api/main.py > /tmp/aegra_boot.log 2>&1 &

# 2) 容器：交给 log driver（JSON 输出可直接解析）
docker run --log-driver json-file --log-opt max-size=10m --log-opt max-file=5 ...

# 3) systemd：交给 journald（在 unit 里限制 journald 保留期）
```

`ENV_MODE` 非 `LOCAL`/`DEVELOPMENT` 时 stdout 也是 JSON，这三种方式都能直接解析。`/tmp` 会被系统清理，需要留档就别放那儿。

---

## 5. 可观测性

### OpenTelemetry（追踪）

| 变量 | 默认 | 说明 |
|---|---|---|
| `OTEL_TARGETS` | 空 | 逗号分隔、大小写不敏感：`LANGFUSE`、`PHOENIX`、`GENERIC`（别名 `DEFAULT`/`OTLP`）。空 = 关闭追踪 |
| `OTEL_SERVICE_NAME` | `aegra-backend` | resource 里的 `service.name` |
| `OTEL_CONSOLE_EXPORT` | `false` | 把 span 打到控制台，便于本地确认埋点是否生效 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` | 无 | `GENERIC` 目标的端点与请求头（`k=v,k2=v2`） |
| `LANGFUSE_BASE_URL` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | `http://localhost:3000` / 无 / 无 | `LANGFUSE` 目标。**两个 key 少一个，这个目标就静默失效**（只留一条 debug 日志） |
| `PHOENIX_COLLECTOR_ENDPOINT` / `PHOENIX_API_KEY` | `http://127.0.0.1:6006/v1/traces` / 无 | `PHOENIX` 目标 |

埋的是 LangChain/LangGraph 执行（`LangChainInstrumentor`），span 上会补 `user.id` / `session.id` 之类的属性；aegra 自身的 HTTP 请求不在追踪范围内。

只要设了目标或有 `OTEL_CONSOLE_EXPORT` 就启用；都没有则启动日志里有一句 `Observability is disabled (no targets configured).`

### Prometheus 指标

| 变量 | 默认 | 说明 |
|---|---|---|
| `ENABLE_PROMETHEUS_METRICS` | `false` | 打开后暴露 `/metrics` |

`/metrics` **没有鉴权**，默认排除 `/health`、`/ready`、`/live`、`/info`、`/metrics`、`/docs` 等路径的统计。除了默认的 HTTP/进程指标，还有 `aegra_reaper_recovered_runs_total` 和 `aegra_thread_ttl_swept_threads_total` 两个自定义计数器。

---

## 6. 排错对照表

| 现象 / 报错 | 原因 | 怎么修 |
|---|---|---|
| 启动即 `Configuration file not found. Expected one of: ...` | 三个候选配置文件都不在 | 在 CWD 放 `aegra.json`，或把 `AEGRA_CONFIG` 指对 |
| 启动即 `JSONDecodeError` | `aegra.json` 语法错误 | 校验 JSON |
| `Invalid graph path format: <path>` | 值里少了 `:` | 写成 `./graphs/x.py:graph` |
| `Graph file not found: <path>` | 路径写错，或用了点号模块名 | 相对配置文件目录写对；不支持 `pkg.mod:graph` |
| `Graph export not found: <name> in <file>` | 导出名写错，或模块没编译出图 | 检查模块里的变量名 |
| `Graph IDs 'a.b' and 'a_b' collide after sanitisation (...)` | 两个 key 清洗后同名 | 改掉其中一个 key |
| 图模块导入报错 → 启动失败 | 图依赖的包/目录不在 `sys.path` | 把目录加进 `dependencies`，或装依赖 |
| `Assistant 'x' not found` | 传的 `assistant_id` 既不是配置里的 graph key，也不是库里已有的 assistant | 用配置里的 key；或先 `/assistants` 建好再传 uuid |
| `Graph 'x' not found for assistant` | 库里 assistant 引用的 graph key 已从 `aegra.json` 删掉 | 把 key 加回配置（重启生效），或改客户端指向现存的图 |
| `Graph 'x' not found in aegra.json. Available: [...]` | 在建 assistant 时传了配置里没有的 `graph_id` | 从提示的可用列表里选 |
| 改了 `aegra.json` 没反应 | 配置只在启动时读一次 | 重启进程 |
| 改了 `config.yaml` 没反应 | ① 只在 import 时读一次；② 同名环境变量优先（旧 `.env` 里还留着 `MAX_*` 那一行） | 重启；`env | grep -E 'MAX_|OKF_|S3_|OUTBOX_'` 看有没有变量盖掉它 |
| 启动即 `ValueError: ... 不是合法 YAML` | `config.yaml` 缩进/冒号写错 | 用 YAML 校验器过一遍（缩进必须空格，不能 Tab） |
| 启动即 `ValueError: 配置 xlsx.max_cells 需要数字` | `config.yaml` 里值写成了非数字（比如 `2m`） | 写成纯数字 |
| `config.yaml` 里加的键不生效 | 键名拼错，或放错了段（`_SPEC` 里没这个键） | 对照 §3.2 的表；单测 `tests/unit/test_project_config.py` 会把拼错的键抳出来 |
| 启动即 `KeyError: 'JWT_SECRET'` / `'PASSWORD_PEPPER'` | `.env` 缺密钥（这两个**不**在 `config.yaml`） | 在 `.env` 补上，见 §3.3 |
| `Custom apps must be FastAPI applications. ...` | `http.app` 导出的不是 FastAPI 实例 | 用 `FastAPI()` 建 app |
| `User has no 'org_id' required for 'orgs'-scoped store access` | 用了 `store.scopes` 但身份里没有该属性 | 在 auth 处理器里补该属性，或去掉这条 scope |
| `must be 'sync', 'async' or 'exit', got ...` | `checkpointer.durability` 取值非法 | 改成三者之一 |
| `ENV_MODE` 改了但日志还是彩色 | 彩色只看 `LOCAL`/`DEVELOPMENT`，其他值才 JSON；且改动需要重启 | 重启进程 |
| 没有 `logs/` 目录 / 没有日志文件 | `LOG_FILE` 为空或目标目录创建失败 | 看启动 warning；确认 `LOG_FILE` 与 CWD 写权限 |
| 日志文件不轮转 | 文件没写满 `LOG_FILE_MAX_MB` | 调小上限，或用外部 logrotate |
| OTEL 配了目标但没有数据 | Langfuse 少了 public/secret key 会静默 no-op | 补齐凭据，开 `OTEL_CONSOLE_EXPORT=true` 先确认埋点 |

---

## 7. 完整示例

`aegra.json`：

```json
{
  "dependencies": ["./examples"],
  "graphs": {
    "agent": "./examples/react_agent/graph.py:graph"
  },
  "http": {
    "app": "./examples/custom_routes_example.py:app",
    "enable_custom_route_auth": true,
    "cors": { "allow_origins": ["https://app.example.com"] }
  },
  "store": {
    "scopes": { "orgs": ["org_id"] },
    "index": { "dims": 1536, "embed": "openai:text-embedding-3-small", "fields": ["$"] }
  },
  "checkpointer": {
    "durability": "async",
    "ttl": { "strategy": "delete", "default_ttl": 10080, "sweep_interval_minutes": 30 }
  },
  "auth": { "path": "./auth.py:auth" }
}
```

`config.yaml`：见仓库根的 [`config.yaml`](../config.yaml)（本项目新增的非密钥参数，带注释，默认值就是代码内默认）。只写要改的键即可。

```yaml
quota:
  max_threads_per_user: 50
s3:
  endpoint: ""                # 空 = 对象存储不启用
okf:
  backend: local
```

`.env`：见仓库根目录的 [`.env_example`](../.env_example)（aegra 原生参数、密钥、第三方 SDK）。**密钥只放这里**。

相关文档：

- 术语表：[`../CONTEXT.md`](../CONTEXT.md)
- async subagent（ACP）契约：[`design.md`](./design.md)
- 本目录索引：[`README.md`](./README.md)
