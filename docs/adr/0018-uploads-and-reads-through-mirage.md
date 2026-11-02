# 上传重建：字节与读路径都可切，集群走 Mirage

> 状态：**已实现**（决定 2026-11-01）。
> 落点：`graphs/access/mirage_backend.py`、`graphs/access/uploads_backend.py`、
> `shared/session/uploads.py`（上传介质层）、`graphs/xlsx_agent.py`、`graphs/prompts/xlsx.md`、
> `graphs/supervisor.py`、`graphs/space_agent.py`、`graphs/access/spaces.py`（`upload_scope`）、
> `graphs/middleware/async_task_guard.py`（`_config_for` 转发上传目录）、
> `custom/api/uploads.py`、`custom/service/uploads.py`、`shared/session/upload_rule.py`、
> `shared/session/blobs.py`（`upload_prefix`/`upload_key`）、`custom/db/models.py`（`target_agent`）、
> 迁移 `a7b8c9d0e1f2` / `b8c9d0e1f2a3`、`scripts/reap_upload_orphans.py`、`config.yaml` 的 `mirage.*` 与 `uploads.*`。
> 取代：[`archive`](./archive/) 里已删的 ADR-0013（会话文件在 Store）与 ADR-0015 的 key 布局；
> `feat/unified-files-s3` 分支上的另一份 ADR-0017（与本仓库**已归档**的
> [0017](./archive/0017-local-backend-scandir.md) 同号不同内容）不采用。

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

### 8. 表格解析一次存 Parquet，查询交给 DuckDB（介质可配）

xlsx 是 zip 里的 XML，每查一次都要重新解压解析：80k×10×3 的表一次全读 41.7 s。
早先的实现把解析结果塞进 SQLite 建索引，解决了"重复查"，但留下三个问题：索引键是
「路径 + mtime + size」，跨机器不通用；**SQLite 依赖文件锁语义**，`cache_dir` 一旦
指向共享卷（或想让多实例共用）就不再成立；只剩一种执行器，跨表 join 要手工搬数据。

所以**删掉 SQLite 这条路**，任何表第一次被查时都解析一次：

```
xlsx 字节（download_bytes 已取到本地）──► 每 sheet 一个 Parquet
                                              │
        本地 <cache_dir>/derived/<sha256>/h<header>/<sheet>.parquet
        远端 <xlsx.cache_prefix>/<sha256>/h<header>/<sheet>.parquet（cache_mode=s3）
```

之后所有 `xlsx_info/query/search/profile` 都走 DuckDB `read_parquet`（列裁剪 + 谓词下推），
四个工具只有一条实现。小表（几百行）解析本身 20ms，为它留一条"不物化"的分支不值得
——多一条路径就多一处要守的行为差异。

四条约束：

1. **按内容和表头命名**：`sha256(xlsx 字节) + header` 决定目录名（`<sha256>/h<header>/`）。
   同一份表在任何实例上解析结果完全一致，可以跨机共享；表改了、或者用不同表头行号
   查同一份文件，自然换新目录，不用失效逻辑。
2. **介质用配置切**：`xlsx.cache_mode` = `local`（默认，只落本机）/ `s3`（多实例共享：
   查询时由 DuckDB 直接 `read_parquet('s3://…')` 边读边算，**不依赖本地缓存目录**——
   任何实例都不必预置或共享磁盘）。`s3` 但没配对象存储时自动降级成 `local`，
   不会因为配置写错而全线报错。**没有 `off`**：不建缓存意味着每次查询重新解析（40 s 级），
   那不是能交给运维去切的旋钮。
3. **落盘必须原子**：`store.write_atomic`（同目录临时文件 + `os.replace` + 冲突重试）。
   两个 worker 同时建同一张表时字节相同，但直接 `write_bytes` 会让读者拿到半截 Parquet；
   Windows 上 `os.replace` 并发替换同一目标还会回 `PermissionError`，所以"对方已写完就
   接受"。清单（`manifest.json`）最后写，看到清单就说明 parquet 齐全。
4. **闸门不放松**：`where` 仍是模型写的 SQL 谓词。两道：`_check_where` 先禁分号、注释与
   `read_*`/`glob`/`attach` 这类名字；`_check_predicate` 再让 DuckDB 把谓词解析成语法树，
   按节点白名单放行（列引用、字面量、比较/逻辑运算、少数函数），**子查询、表函数、
   陌生函数一律拒**。`local` 模式另外关掉 `enable_external_access` 并把 `allowed_directories`
   限定到派生目录；`s3` 模式要直读对象存储，外部访问必须开着，此时注入面全压在语法树
   白名单上（ADR-0018 §10）。`columns` 只按清单里的列名过滤，标识符统一 `_ident` 转义。
   DuckDB 没有 SQLite authorizer 那样的细粒度钩子，靠的就是这两层。

清理：本机缓存按 `xlsx.cache_local_max_mb`（默认 2GB）按最久未改动淘汰；对象存储里的
派生对象用 `scripts/reap_upload_orphans.py --xlsx-cache-days N --apply` 按 LastModified
清（内容寻址、可重建，删了只是下次多解析一遍，不会丢数据）。升级后旧的
`<cache_dir>/*.sqlite`（老索引文件）已经没人读，可以直接删。

### 9. 单机也能跑：上传介质与读路径解耦

对象存储不是所有部署的前提。单机 / 内网 demo / 评测机常常只有一个进程和一块盘，
让人先架一套 S3 才能上传文件是说不过去的。所以把「字节存在哪」单独拎出来成一个旋钮，
和「知识空间怎么读」正交：

| `okf.backend` | `uploads.store` | 上传字节落在 | 读 `/uploads` 的路径 |
| --- | --- | --- | --- |
| `local`（缺省） | `fs` | `data/uploads/<owner>/<thread>/` | `LocalBackend` 直读，`CompositeBackend` 路由到 `/uploads/` |
| `s3`（镜像） | `s3` | 对象存储 `uploads/…` | 首次读拉到同一本地目录再直读（`materialize`） |
| `s3fs` | `s3`（强制） | 对象存储 `uploads/…` | Mirage 的 `S3VFS` 直读，不经过本模块 |

三条约束：

1. **介质按配置选**：`uploads.store` = `auto`（默认，配了 `s3.endpoint` 就是 `s3`，否则 `fs`）
   / `fs` / `s3`。同一份代码在单机和集群上都跑，切换只改配置，不换分支、不装可选依赖。
2. **本地读路径不引入 Mirage**：`local` 模式下 `/` 仍是 `SpaceBackend`（ADR-0005 / [ADR-0017](./archive/0017-local-backend-scandir.md)），
   `/uploads/` 是 `graphs/access/local_backend.py` 的只读 `LocalBackend`，用
   `CompositeBackend` 拼起来。基准（决定 3 与背景）已经说清楚：本地盘上 Mirage 更慢
   （单文件读 0.91ms vs 0.53ms，`grep` 慢 19–38×），没有理由为一致性付这个钱。
3. **组合错了要吵起来**：`s3fs` 只看得见对象存储里的 `uploads/`，所以
   `uploads.validate()` 在这个组合下强制 `store == s3`，否则建模时直接抛错——
   否则症状是「目录明明是空的」，排查成本远高于一次启动失败。

介质层是 `shared/session/uploads.py` 一个文件：`put_stream`（本地写同目录临时文件再
`os.replace`，对象存储走 `blobs.put_stream`）、`materialize`（`fs` 直接返回、`s3` 缺本地
副本时拉一份）、`delete` / `delete_thread` / `iter_local_keys`。两种介质下
台账（`session_object`）、路由（`upload_rule`）、配额、删会话清理完全共用一套代码。
清理脚本 `scripts/reap_upload_orphans.py` 两种介质都扫：对象存储按 `LastModified`、
本地按文件 mtime，都带 24h 窗口，避免删掉「字节已落、行未写」的在途上传。

## 代价与已知风险

- **`grep` 慢**：L 档全库 grep 1839ms（S3）vs 宿主 ripgrep 351ms；S 档快（8.6ms vs 22.3ms）。
  真要做大范围检索，应绕开 Mirage 用宿主 ripgrep，再把命中文件交给 Mirage 读。
- **每 run 冷启动 200–400ms**：能接受是因为池会复用；单次会话首次提问会感觉到。
- **没有 `execute`**：这是刻意的（见决定 2）。要跑代码只能靠 `CodeInterpreterMiddleware`
  的量化解释器，不能拿到 shell。
- **两个 S3 client**：`/` 和 `/uploads` 各一个 `S3VFS`（两套连接池），每 run 两个。
- **只有异步方法**：Mirage 适配层不实现同步 `ls/read/...`，deepagents 侧一律走 `a*`。
- **本地盘模式读路径没换**：`okf.backend: local` 仍走 `SpaceBackend`（ADR-0005 / [ADR-0017](./archive/0017-local-backend-scandir.md)），
  上传目录由 `LocalBackend` 挂在 `/uploads/`（ADR-0018 §9），不上 Mirage。
- **本地上传目录不是共享存储**：`uploads.store=fs` 下多实例各存各的盘，会话被调度到
  另一台就看不见自己的文件。多实例必须用 `s3`；这一点靠部署文档约束，代码不探测。
- **派生缓存要占第二份存储**：Parquet 通常比 xlsx 小（列裁剪 + ZSTD），但 80k 行级别
  仍是 MB 级；`s3` 模式下对象数随"表 × sheet × header"增长，靠 TTL 回收。
- **每次都先付解析费**：没有"不物化"的捷径，小表也要写一次 Parquet（20ms 级）。
  这是删掉 SQLite 换来的单一实现代价。
- **首次查询付解析费**：任何实例没建过这份派生文件时，要解析 + 上传；`s3` 模式的查询
  每次走对象存储（列裁剪 + 谓词下推，Range GET），结果集小的时候比读本机盘慢一截，
  但没有"本地副本过期"这种状态。
- **DuckDB 没有细粒度 authorizer**：`allowed_directories` 挡不住所有表函数，`s3` 模式下
  外部访问还必须开着，真正的护栏是 `_check_predicate` 的语法树白名单。改这块代码要
  一起改测试。

## 被否决的备选

1. **沿用 `feat/unified-files-s3` 的 `_ObjectTree`**：能用，但要自己维护 `ls/read/glob/grep`
   四套语义 + 缓存失效，且基准显示 S3 上比 Mirage 慢（`glob_all` L 档 3.5s vs 479ms）。
2. **把 Mirage 当上传写入口**（`awrite` 直写对象存储）：上传要配额、sha、台账、分片清理，
   而 Mirage 的写路径 `NotImplementedError`（`delete`）/ `already exists`（`write`），
   不适合做 HTTP 入口。写用 boto3，读用 Mirage，边界清楚。
3. **Deep Agents 的 `StoreBackend`**：文件进 PG 的 Store，大对象不合适（ADR-0016 的问题）。
4. **每 run 新建 Workspace**：语义最干净，但冷启动每轮都付，且拿不到 run 收尾钩子时
   泄漏的连接比池化更难排查。
