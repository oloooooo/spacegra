# 本地 backend 改成「一趟 scandir」，不再逐条 resolve

> 状态：**已实现**（决定 2026-10-31）。
> 落点：`graphs/access/local_backend.py`（`LocalBackend`）、`tests/unit/test_local_backend.py`。

## 背景

2026-10-31 的 backend 基准（`fs` / `space` / `mirage-ram` / `mirage-redis`，S=25 / M=1020 /
L=10040 文件，独立进程、warmup 3 + N 30）暴露出一个与 backend 选型无关的事实：
**本地臂的 `glob` 是全场最慢的一环**——L 档 `glob(**/*.md)` 3.86 s、`glob(**/*)` 3.94 s，
比 Mirage 的 2.45 s 还慢 1.5 倍。

cProfile（5 s 样本，同一次 glob）给出唯一答案：

| 热点 | 次数 | 累计 |
|---|---|---|
| `nt._getfinalpathname` | 36998 | **2.06 s（41%）** |
| `Path.relative_to` | 28343 | 0.86 s |
| `Path.is_file` | 9844 | — |
| `Path.stat` | 9249 | — |

`FilesystemBackend` 的每个条目都要走两次 `Path.resolve()`（一次在 `_resolve_path`，
一次在 `_to_virtual_path` 里）来做「不许越界」的校验。也就是说：**慢的不是磁盘**——
裸 `rglob('*')` 遍历同样 10759 个条目只要 52.8 ms，`os.scandir` 手写 24.1 ms，
`rg --files` 29.4 ms。慢的是每条约 0.14 ms 的安全校验。

## 决定

### 1. `LocalBackend` 是 `FilesystemBackend` 的子类，只覆盖同步方法

`BackendProtocol` 的 `als/aread/aglob/awrite/aedit/adelete` 默认实现都是
`asyncio.to_thread(同步方法)`，而 deepagents 0.7.23 里只有 `agrep` 有异步特化
（它要跑 ripgrep 子进程）。所以**只改同步方法**就够了，异步面自动跟上。

### 2. 每次调用只 resolve 一次，之后纯 `os.scandir`

- 校验起点：`_resolve_path(path)` 一次（保留父类的越界判定，含 `..` 与根外路径）；
- 枚举：`os.scandir` + `entry.is_dir(follow_symlinks=False)` / `entry.stat(follow_symlinks=False)`，
  这两次读的是目录枚举时已经拿到的 `FindFirstFile` 数据，不再触发新 syscall；
- 虚拟路径：`_prefix_of(fs_path)` 拿一次目录前缀，`_display(rel)` 拼 `/xxx`，不再逐条 `relative_to`。
- glob 语义完全复用父类的 `compile_grep_include_glob(pattern)`（裸模式匹配 basename、含 `/`
  匹配相对路径、`..` 报错），只把「遍历」换成显式栈 DFS，保留 `glob_timeout` 预算截断。

### 3. 不跟随符号链接

`is_symlink()` 为真的条目直接跳过。这不只是安全：父类逐条 `resolve()` 会把 `/link.md`
「透明」成它指向的 `/a.md`，同一个文件在 `ls`/`glob` 里出现两次（实测）。
本类因此**结果更干净**，代价是不支持「用链接把内容挂进空间」——本服务没有这种用法。

### 4. `grep` 一行不改，继续用父类的 ripgrep

基准里 `grep` 在三档都是 fs ≈ space ≈ local（L 档 350 ms / 1020 命中），
远快于 Mirage 的 13.4 s。ripgrep 已经是这块的天花板，没必要自己写。

### 5. 默认只读（`allow_write=False`）

`write/edit/delete/upload_files` 一律返回 `read-only local backend: … rejected`。
空间知识对 agent 本来就是只读的（`SpaceBackend` 也拒绝写）；需要可写时显式
`LocalBackend(root, allow_write=True)`，此时委托父类。

### 6. 把「越界」和「二进制」变成错误字符串，而不是异常

两个都是父类的真实缺陷，在这次实现里顺手修掉：

- `_resolve_path` 在越界/`..` 时抛 `ValueError`，而父类 `ls/read/glob` 只 catch
  `(OSError, RuntimeError)` → 异常会一路冒泡到中间件。本类三个入口都补上 `ValueError`，
  返回 `Error reading file '/../etc/passwd': Path traversal not allowed` 这类结构化错误。
- `_get_file_type` 把 `.xlsx` 判成 text，父类 `read` 拿到 GBK 字节就抛
  `UnicodeDecodeError`。本类改为：utf-8 解码失败或前 8 KiB 含 NUL → 返回
  `File '…' is binary (N bytes, …); read() only returns UTF-8 text — use a dedicated tool
  (e.g. read_xlsx) instead`。父类对 NUL 字节不出错（原样把 `\x00` 丢给模型），本类也拦。

### 7. 读文件按声明大小读

`read` 先 `stat` 拿到 `size`（顺带做 `max_read_bytes` 上限），再 `handle.read(size)`。
第一版写的是 `handle.read(max_read_bytes + 1)`（32 MB + 1），CPython 会为此分配 32 MB 缓冲
并做一次大尺寸 IO，**把 1.5 KB 的读从 0.02 ms 拖到 5.7 ms**（实测，比父类慢 20 倍）。

## Considered Options

| 方案 | 为什么不选 |
|---|---|
| 直接改 `deepagents` 的 `FilesystemBackend` | 上游行为受 `virtual_mode` 兼容性约束；fork 后每次升级都要重新对账 |
| 用 `rg --files` + 自己匹配 | 每次调用多一个进程（20–30 ms），S 档（25 文件）反而更慢；且 `rg` 的匹配语义与 deepagents 的 matcher 不一致 |
| 缓存目录树 + mtime 失效 | 需要处理失效竞态与内存上限，收益只有 ms 级，属过度设计 |
| 换 Mirage（`LangchainWorkspace`） | 基准已否定：`grep` 慢 19–38×、`delete` 未实现；`glob` 虽快于 fs，仍比本类慢 33× |

## 后果

**拿到的**（L 档 10040 文件，p50；倍率对 `FilesystemBackend`）：

| op | fs | local | 倍率 |
|---|---|---|---|
| `glob **/*.md` | 184.18 ms | **3.74 ms** | **0.02×（49×）** |
| `glob **/*` | 3770.93 ms | **72.24 ms** | **0.02×（52×）** |
| `grep` 单空间 | 34.72 ms | 35.50 ms | 1.02× |
| `read` | 0.47 ms | 0.46 ms | 0.98× |
| 冷启动 | 6.1 ms | **1.1 ms** | 0.18× |
| 工具层 `ls` | 26.07 ms | **0.67 ms** | 0.03× |
| 工具层 `glob` | 25.98 ms | **0.91 ms** | 0.04× |

命中数、`is_dir`、`size`、`modified_at` 与父类逐条相同（M 档 710→711 条那个差是
旧数据里的一个探针残留文件，不在 S/L 两侧出现）。

**要接受的**：

- 符号链接不再可见（含指向根内的链接）；父类会把它们解析成目标文件。
- 目录项的 `modified_at` 取自 `scandir` 的枚举快照，NTFS 会延迟刷新目录时间戳，
  与 `Path.stat()` 可能相差几百微秒（测试里因此只对文件比时间戳）。
- 仍是**虚拟路径**语义：`virtual_mode=False` 时行为回落到父类（绝对路径）。

**什么时候该换成 FS 之外的方案**：空间内容挪到对象存储后（ADR-0016 的 `s3` 后端读的是
本地镜像，所以本类仍然适用）；若哪天直接对 S3 做 glob，这里的 scandir 假设不再成立。

**接入状态（2026-10-31）**：**暂不接生产**。`graphs/access/backend.py` 的 `SpaceBackend` 与
`graphs/supervisor.py` 保持原样，本类目前只被基准脚本与 `tests/unit/test_local_backend.py` 使用。
要启用时最省事的一步是让 `SpaceBackend` 继承 `LocalBackend`（ACL 逻辑不动），代价是多一层继承。

## 怎么验证（本地）

```bash
.venv/Scripts/python.exe -m pytest tests/unit/test_local_backend.py -q     # 28 passed
.venv/Scripts/python.exe -m pytest tests/unit/test_layout.py tests/unit/test_xlsx_scope.py -q
```

基准脚本在系统 temp（`bench.py run --arm local --ds L --n 30`），不入库。
