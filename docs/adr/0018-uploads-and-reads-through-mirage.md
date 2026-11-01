# 上传重建：对象存储是唯一真相，读路径全走 Mirage

> 状态：**已实现**（决定 2026-11-01）。
> 落点：`graphs/access/mirage_backend.py`、`graphs/xlsx_agent.py`、`graphs/prompts/xlsx.md`、
> `graphs/supervisor.py`、`graphs/space_agent.py`、`graphs/access/spaces.py`（`upload_scope`）、
> `graphs/middleware/async_task_guard.py`（`_config_for` 转发上传目录）、
> `custom/api/uploads.py`、`custom/service/uploads.py`、`shared/session/upload_rule.py`、
> `shared/session/blobs.py`（`upload_prefix`/`upload_key`）、`custom/db/models.py`（`target_agent`）、
> 迁移 `a7b8c9d0e1f2` / `b8c9d0e1f2a3`、`scripts/reap_upload_orphans.py`、`config.yaml` 的 `mirage.*`。
> 取代：[`archive`](../adr/) 里已删的 ADR-0013（会话文件在 Store）与 ADR-0015 的 key 布局；
> `feat/unified-files-s3` 分支上的 ADR-0017（同名不同内容）不采用。

## 背景

main 上原来的上传能力被整体摘掉（`POST /custom/uploads`、`session_object` 的读写、`SessionFilesMiddleware`），
底座留在原地：`shared/session/blobs.py`、`session_object` 表、`s3.*` / `session_files.*` 配置。

两个 `feat/unified-files-*` 分支各自重做了一套读路径，都是**自己实现 backend**：
`_ObjectTree` 把 `ls/read/glob/grep` 写成"给一份 `{vpath: 对象}` 就能算出来"的纯函数。
那套代码要在同一个类里同时维护路径判定、权限、对象存储调用、缓存，四个方法各自演化。

同时 `mirage-ai` 进了依赖（`mirage-ai[s3]==0.0.7`，见 `pyproject.toml` 与 `boto3` 1.40 线）。
它提供的东西正好是上面那堆手写代码：VFS 层（`S3VFS`）、`find`/`grep`/`ls` 等命令、缓存与索引、
按会话的可见性策略。2026-11-01 的基准（`fs` / `space` / `mirage-ram` / `mirage-redis` / `mirage-s3`，
S=25 / M=1020 / L=10040 文件）给出的结论是：**S3 上 Mirage 明显更快，本地盘上反而更慢**——
列举类 S3 快 5–8×（`glob_all` L 档 479ms vs 3771ms），单文件读快 2×（0.22ms vs 0.53ms），
但 `grep` 是 Python 逐对象扫，比宿主 ripgrep 慢 5×（`grep_all` L 档 1839ms vs 351ms）。

## 决定

### 1. 对象存储是唯一真相，布局只有两个前缀

- `okf/<space>/...` —— 空间知识（ADR-0016 沿用）；
- `uploads/<owner>/<thread>/<文件名>` —— 一个会话的上传文件。

`owner`/`thread` 直接进 key（不摘要）：这个前缀同时是 S3 mount 的 `key_prefix`，
摘要会让人对不上号。同名上传就是覆盖（PUT 语义，不是版本链）。

### 2. 读路径只有一个实现：每 run 一个 Mirage Workspace

`graphs/access/mirage_backend.py::build_workspace` 挂两个目录：
`/` = `S3VFS(key_prefix="okf")`，`/uploads` = `S3VFS(key_prefix="uploads/<owner>/<thread>")`，
`mode=MountMode.READ`，缓存/索引用 Redis（`mirage.redis_url`，空则进程内）。

`MirageBackend` **只继承 `BackendProtocol`，不继承 `SandboxBackendProtocol`**：
后者会多出 `execute`/`aexecute`/`id`，deepagents 会把 `execute` 当工具交给模型，
等于把 shell（含 `python3`/`js` 解释器）开给 agent。`__getattr__` 对这三个名字抛 `AttributeError`
兜底；其余属性透传 `LangchainWorkspace`。

### 3. 可见性用 profile，不用代码判断

每个 agent 一个**具名 profile**：`hide ["/"]` + `show ["/<自己的 space>", "/uploads"]`，
主 agent 额外 `show ["/process.md"]`（ADR-0005 的根共享文件）。未 show 的路径整个消失：
`ls` 抛 `TypeError`、`read` 返回 `None`，适配层把这两种表现归一成空列表与
「找不到文件: `/x`」，不让模型看到裸异常。

**不能**把范围写成 inline `permissions`：Mirage 只允许 inline 加 `ask`/`deny`/`hide`，
`show` 必须来自 profile（`PolicyError: inline permissions may add ask and deny rules and hides, not show entries`）。

### 4. Workspace 按会话复用（TTL 池），不是每 run 新建

工厂图（`graphs/supervisor.py::graph`）确实每请求重建一次，但它只建**图对象**，
拿不到 run 结束的钩子：aegra 只是 `async with get_graph(...)` 把图交出去执行。
所以按 `(owner, thread, 可见范围指纹)` 缓存，`TTL 300s`、上限 8 个，
`custom/app.py` 的 lifespan shutdown 调 `close_all_workspaces()` 统一关（否则留下未关闭的
aiohttp 连接）。代价是每 run 冷启动 ~200–400ms（建 S3 client + 首次 LIST），
换来同一会话连续提问不重复付这笔钱。

### 5. 台账只做归属与配额，读路径零查询

`session_object(owner, thread, name)` 回答"这个会话里有哪些文件、该派给谁"，
镜像直接按前缀列对象，**不查这张表**。新增 `target_agent` 列存上传那一刻算好的投递目标
（`shared/session/upload_rule.route_upload`）：`<空间名>_xxx.xlsx` 且空间在授权内 → 该空间 agent；
其余 xlsx（含越权前缀）→ 通用 `xlsx` agent；md 等 → 主 agent。读路径不重算，
避免上传者权限变化后同一份文件被换个 agent 处理。

删会话时靠外键 `ON DELETE CASCADE` 删行（`b8c9d0e1f2a3`）；对象存储里的字节由
`scripts/reap_upload_orphans.py` 按"前缀没有台账行 + 对象超过 24h"清掉——不在删除事务里做网络 IO。

### 6. 写路径仍是 boto3 直写，Mirage 不参与

`POST /custom/uploads` → `blobs.put_stream`（边写边算 sha256，超限自己 abort 分片）→
upsert 台账行。顺序不可换：先字节后行，崩在中间只会多一个没人认领的对象；
反过来会出现台账指向不存在的字节。行写失败时尽力把刚写的对象删掉，让状态回到"两边都没有"。

### 7. 表格分析从主 agent 挪进子 agent

主 agent **不再持有 xlsx 工具**：它看得见所有上传文件（`show /uploads`）但不分析表格，
命中失败或越权的表格一律派给通用 `xlsx` 子 agent（`graphs/xlsx_agent.py`，
profile 只 `show /uploads`，工具 = `XLSX_TOOLS` + 量子解释器）。
空间 agent 保留自己空间的表格工具（`has_tables(space)`），因为它的数据就在自己空间里。

子 run 的身份是服务账号、线程是子线程，直接用会看不到父会话的上传目录；
`_config_for` 把父会话的 `upload_owner`/`upload_thread` 转发进子 run config，
`spaces.upload_scope(config)` 优先读它们。

## 代价与已知风险

- **`grep` 慢**：L 档全库 grep 1839ms（S3）vs 宿主 ripgrep 351ms；S 档快（8.6ms vs 22.3ms）。
  真要做大范围检索，应绕开 Mirage 用宿主 ripgrep，再把命中文件交给 Mirage 读。
- **每 run 冷启动 200–400ms**：能接受是因为池会复用；单次会话首次提问会感觉到。
- **没有 `execute`**：这是刻意的（见决定 2）。要跑代码只能靠 `CodeInterpreterMiddleware`
  的量化解释器，不能拿到 shell。
- **两个 S3 client**：`/` 和 `/uploads` 各一个 `S3VFS`（两套连接池），每 run 两个。
- **只有异步方法**：Mirage 适配层不实现同步 `ls/read/...`，deepagents 侧一律走 `a*`。
- **本地盘模式没换**：`okf.backend: local` 仍走 `SpaceBackend`（ADR-0005 / ADR-0017），
  上传目录在本地模式下不存在，`xlsx` 子 agent 也不挂。

## 被否决的备选

1. **沿用 `feat/unified-files-s3` 的 `_ObjectTree`**：能用，但要自己维护 `ls/read/glob/grep`
   四套语义 + 缓存失效，且基准显示 S3 上比 Mirage 慢（`glob_all` L 档 3.5s vs 479ms）。
2. **把 Mirage 当上传写入口**（`awrite` 直写对象存储）：上传要配额、sha、台账、分片清理，
   而 Mirage 的写路径 `NotImplementedError`（`delete`）/ `already exists`（`write`），
   不适合做 HTTP 入口。写用 boto3，读用 Mirage，边界清楚。
3. **Deep Agents 的 `StoreBackend`**：文件进 PG 的 Store，大对象不合适（ADR-0016 的问题）。
4. **每 run 新建 Workspace**：语义最干净，但冷启动每轮都付，且拿不到 run 收尾钩子时
   泄漏的连接比池化更难排查。
