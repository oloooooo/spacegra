# 启动说明

仓库有两条可运行的线，**启动步骤几乎一样，差别只在要不要 S3**：

| | `mirage-s3`（S3 线） | `local-spacebackend`（本地线） |
|---|---|---|
| 知识空间 | 直读对象存储（SeaweedFS / 真 S3） | 直读仓库 `knowledge/` |
| 上传字节 | `s3://aegra-blobs/uploads/<owner>/<thread>/` | `data/uploads/<owner>/<thread>/` |
| 表格派生缓存 | 对象存储 `xlsx-derived/<sha256>/h0/`，多实例共享 | 本机 `.cache/xlsx/derived/` |
| S3 是否必须 | **必须**（没有 S3 用不了） | 不需要（S3 只用来把知识推给别的线） |
| 上传接口 | 多文件 `files`，部分失败返回 207 | 单文件 `file` |
| 前端上传按钮 | 有 | 没有（只能用 curl / 脚本） |

> 两条线**共用同一个 `.env` 和同一个 PG 库**（默认 `aegra`）。切分支只是换代码，数据目录会混在一起：在 A 线上传的文件，B 线看不到（一个在 S3、一个在本机盘）。

---

## 0. 两条线共同的准备

### 0.1 环境

用仓库自带的虚拟环境，别用系统 python（这台机器上 PATH 里的 `python` 是 WindowsApps 占位符）：

```cmd
cd /d E:\code_\aegra-api
set PYTHONPATH=E:\code_\aegra-api
```

后续命令都假设已经 `cd` 到仓库根、且设好了 `PYTHONPATH`。

### 0.2 `.env`（未跟踪，两条线共用一份）

`.env` 不进 git，需要自己准备。必须有这些键：

```dotenv
# Postgres（compose.yaml 里没有 PG，用宿主已有实例）
POSTGRES_USER=postgres
POSTGRES_PASSWORD=1234
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=aegra

# 模型
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
OPENAI_MODEL=...

# 安全密钥（localhost 的 dev 值别拿去上线）
JWT_SECRET=...
PASSWORD_PEPPER=...
SESSION_SCOPE_SECRET=...

# 服务
HOST=127.0.0.1
PORT=2026
AUTH_TYPE=custom
REDIS_BROKER_ENABLED=true          # 不开这个，后台任务/流式会直接报 "Redis not initialized"
REDIS_URL=redis://127.0.0.1:6379/0
WORKER_COUNT=2                     # worker 进程数（uvicorn --workers 跟上面对齐）
N_JOBS_PER_WORKER=10               # 每个 worker 里的执行槽位数
```

S3 凭证（只有 S3 线、或本地线要推知识时才需要）：

```dotenv
S3_ACCESS_KEY=dev                  # 不配默认就是 dev/dev，接真 S3 才必须写
S3_SECRET_KEY=dev
```

### 0.3 数据库迁移

```cmd
.venv\Scripts\python.exe -m alembic upgrade head
```

### 0.4 起 Redis + 对象存储容器

```cmd
docker compose up -d
```

`compose.yaml` 只起两个容器（PG 不在这里）：`redis:6-alpine`（`127.0.0.1:6379`）和 `chrislusf/seaweedfs:latest`（S3 网关 `127.0.0.1:8333`）。端口被占时可覆盖：`set SEAWEEDFS_S3_PORT=18333&& docker compose up -d`。

Git Bash 里跑要加 `MSYS_NO_PATHCONV=1`，否则 `/data` 会被改写成 Windows 路径。

### 0.5 起 API

```cmd
.venv\Scripts\python.exe -m uvicorn aegra_api.main:app --app-dir src --host 127.0.0.1 --port 2026 --loop asyncio:SelectorEventLoop --workers 2
```

- `--app-dir src` 必须加（包在 `src/` 下）
- `--loop asyncio:SelectorEventLoop` 必须加：psycopg/asyncpg 跟 Windows 默认的 Proactor 事件循环不兼容
- 改了 Python 代码要**重启**（没开 `--reload`）
- 自检：`curl http://127.0.0.1:2026/health` → 三个 `connected`

### 0.6 起前端

```cmd
cd web
npm install          :: 第一次
npm run dev -- --port 5273
```

打开 **`http://localhost:5273`**（vite 只监听 `localhost`，写 `127.0.0.1:5273` 连不上）。

### 0.7 超管账号

超管只能直接改库：先注册，再把 `is_super` 打开。

```cmd
:: 1) 注册
curl -X POST http://127.0.0.1:2026/custom/register -H "Content-Type: application/json" -d "{\"identity\":\"root\",\"display_name\":\"root\",\"password\":\"1234\"}"

:: 2) 提权
psql -h localhost -U postgres -d aegra -c "UPDATE user_tables SET is_super = true WHERE identity = 'root';"
```

登录接口字段是 `identity` / `password`（不是 `username`）：`POST /custom/login`。

---

## 1. S3 线（分支 `mirage-s3`）

```cmd
git checkout mirage-s3
docker compose up -d                      :: Redis + SeaweedFS S3 网关 8333
.venv\Scripts\python.exe -m alembic upgrade head
python scripts\okf_push_s3.py             :: 一次性：把 knowledge/ 推到 okf/ 前缀（不推 = 空间全空）
.venv\Scripts\python.exe -m uvicorn aegra_api.main:app --app-dir src --host 127.0.0.1 --port 2026 --loop asyncio:SelectorEventLoop --workers 2
```

### 1.1 这条线的关键配置（`config.yaml`）

```yaml
s3:
  endpoint: "http://127.0.0.1:8333"   # 空 = 不启用；必须与 compose 的 S3 网关一致
  bucket: aegra-blobs
  region: us-east-1

okf:
  backend: s3fs                        # local | s3 | s3fs；s3fs = Mirage 直读对象存储
  dir: knowledge

mirage:
  redis_url: redis://127.0.0.1:6379/0  # 目录索引缓存
  cache_limit: 256MB
  index_ttl_seconds: 600

xlsx:
  cache_mode: s3                       # s3 = 派生写对象存储并直读；local = 只落本机
  cache_prefix: xlsx-derived
  convert_timeout_seconds: 120         # 上传时同步转换的超时，超了算上传失败

session_files:
  max_upload_mb: 10                    # 单文件
  max_batch_mb: 50                     # 一次请求的总字节
  max_files_per_session: 50
  manifest_max_entries: 20             # 每轮注入系统提示的文件清单条数上限（0 = 关）
```

优先级：**同名环境变量 > `config.yaml` > 代码默认值**（`shared/config.py`）。类型写错直接启动失败。

### 1.2 想彻底关掉 S3

两个地方一起改，只改一个会炸：

```yaml
s3:
  endpoint: ""        # 1) 置空
okf:
  backend: local      # 2) 必须跟着改回 local（s3fs 留空会直接报错）
```

### 1.3 上传（多文件，前端有按钮）

```cmd
TOKEN=...   :: 先 POST /custom/login 拿 access_token
curl -X POST http://127.0.0.1:2026/custom/uploads ^
  -H "Authorization: Bearer <token>" ^
  -F "thread_id=<会话 id>" ^
  -F "files=@销量.xlsx" -F "files=@说明.md"
```

返回 `{files:[{name,status,sha256,target_agent,virtual_path,sheets?}]}`：全成功 `201`，有失败 `207`（`status:"failed"` + `error`）。xlsx 在上传那一刻就转好（坏文件/超限当场 422 并回滚，不留字节）。

---

## 2. 本地线（分支 `local-spacebackend`）

```cmd
git checkout local-spacebackend
docker compose up -d                      :: 只需要 Redis；SeaweedFS 可以不起
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m uvicorn aegra_api.main:app --app-dir src --host 127.0.0.1 --port 2026 --loop asyncio:SelectorEventLoop --workers 2
```

### 2.1 这条线的关键配置（`config.yaml`）

```yaml
okf:
  dir: knowledge        # 知识空间直接读仓库里的 knowledge/（没有 okf.backend 这个键）

uploads:
  dir: data/uploads     # 上传字节落本机盘（必须在 okf.dir 之外）

s3:
  endpoint: ""          # 只有 scripts/okf_push_s3.py 会用到；留空不影响运行

xlsx:
  cache_dir: .cache/xlsx      # 派生 Parquet 在本机
  query_budget_ms: 5000
  sql_policy: strict
  cache_local_max_mb: 2048    # 本机派生缓存总预算
```

### 2.2 上传（单文件，没有前端入口）

```cmd
curl -X POST http://127.0.0.1:2026/custom/uploads ^
  -H "Authorization: Bearer <token>" ^
  -F "thread_id=<会话 id>" ^
  -F "file=@销量.xlsx"
```

- 字段是 **`file`**（单数），一次一个
- 会话正在跑（`pending`/`running`）时返回 **409**，等这轮结束再传
- 字节落在 `data/uploads/<owner>/<thread>/<文件名>`，agent 里用 `/uploads/<文件名>` 引用
- 想批量脚本化：`python scripts\e2e_uploads_local.py`

### 2.3 这条线的人才用 S3？

只有一件事：把仓库 `knowledge/` 推给别的产品线（`scripts/okf_push_s3.py`）。要推就配 `s3.endpoint` + `.env` 的 `S3_ACCESS_KEY`/`S3_SECRET_KEY`，别动 `okf.dir`。

---

## 3. 常见问题

| 现象 | 原因 |
|---|---|
| 启动即 `Redis not initialized` | `.env` 的 `REDIS_BROKER_ENABLED` 没开或 Redis 没起 |
| `POST /custom/uploads` → 503 | S3 线没配对象存储；本地线不该出现 |
| 空间列表是空的（S3 线） | 没跑 `scripts/okf_push_s3.py` |
| `xlsx` 工具读 `/空间名/x.xlsx` 报 `FileNotFoundError` | 本地线已修（虚拟路径要过 `store.resolve_local`）；旧分支会把它当盘相对路径 |
| 前端连不上 `127.0.0.1:5273` | 用 `http://localhost:5273` |
| 改了代码没生效 | uvicorn 没开 `--reload`，重启进程 |
| 表格查不到刚上传的表 | 上传时转换失败会被回滚（422）；看返回的 `error` |
| 派生文件被人删了 | 下次查询自动重建（原件还在）；清单在 `xlsx-derived/<sha>/h0/manifest.json` |

更细的对象存储配置与排错见 `docs/S3.md`，配置项全表见 `docs/configuration.md` §3.2。
