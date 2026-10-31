# 空间内容进对象存储，backend 由 env 二选一

> 状态：**已实现**（决定 2026-10-25；2026-10-26 复核代码后订正此状态行——之前一直写着「实现未开始」）。
> 落点：`shared/knowledge/root.py`（`local` 直读 / `s3` 拉本地镜像，`pull`/`push`）、
> `scripts/okf_push_s3.py` 与 `scripts/okf_pull_s3.py`、`config.yaml` 的 `okf.backend`。
> **布局变更**：2026-10 起本地数据目录从 `graphs/okf/` 移到仓库根 `knowledge/`（`okf.dir: knowledge`），
> 本文正文里的 `graphs/okf/` 指同一处；S3 前缀仍是 `okf/`，不用重传。

## 背景

空间知识现在在 `graphs/okf/<space>/`（本地目录，总计 145 K），由 `SpaceBackend` 继承
deepagents 的 `FilesystemBackend` 读取，8 个 fs 工具全部经它。两个问题：

1. **多副本不一致**：本地盘挂载在多实例部署下每台看到的不是同一份内容。
2. **上传的 xlsx 无处安放**：ADR-0015 把字节放进了对象存储，空间这边还是盘，两套来源。

## 决定

### 1. 空间内容进 S3，前缀 `okf/<space>/...`

`graphs/okf/` 保留为**开发副本**与**上传源**，不再是生产真相。

### 2. `OKF_BACKEND` env 二选一：`local` | `s3`

- `local`（缺省，本地开发）：直接读 `graphs/okf/`；
- `s3`：读 S3 `okf/` 前缀的**本地镜像**（`OKF_MIRROR_DIR`，缺省 `.cache/okf`），
  镜像缺失或 `OKF_MIRROR_TTL_SECONDS` 到期时自动拉一次。

两条实现共用同一个 `SpaceBackend` 与同一套路径判定（`_check_read` / `_check_write` /
内部驱逐前缀仍是单点）。差别只在 `root_dir` 指向哪里：`graphs/okf` 或镜像目录。

> **决定变更（2026-10-25 实现时）**
>
> 初稿写的是“手写一个 `BackendProtocol` 的只读 S3 实现”。实现时否掉了：协议有 14 个方法
> （`ls` / `read` / `grep` / `glob` / `write` / `edit` / `delete` / `upload` / `download` 加各自的 async 版），
> 而 `ls` 的 virtual_mode 语义、`grep` 的 glob 契约、`read` 的分页与二进制判定都很细。
> 重写一遍的正确性风险远大于收益，而且任何偏差都会**静默**改变 agent 行为（读到少一行、
> 漏一个命中，没人会报错）。镜像方案换来同样的性质（多副本看到同一份空间内容）且
> 零语义风险：8 个 fs 工具走的还是已经在用的那份实现。
>
> **升级触发**：空间大到不能整体镜像（闀值 ~100 MB）或要求“改完立即对所有副本生效”。

### 3. 只读（Q6a 不变）

写全部 deny。`SpaceObjectBackend` 的 `write_file` / `edit_file` / `delete` 一律返回拒绝，
与现在的 `permissions=[FilesystemPermission(operations=["write"], paths=["/"], mode="deny")]` 一致。

### 4. 两个脚本

- `scripts/okf_push_s3.py`：`graphs/okf` → S3 `okf/`，幂等（大小相同跳过），`--dry-run` / `--prune`；
- `scripts/okf_pull_s3.py`：S3 `okf/` → 本地镜像，同样幂等（`knowledge_root()` 自动调用它）。

### 5. 为什么自己写

deepagents 0.7.23 的 `backends/` 里没有 S3；PyPI 0.7.32 的依赖里也没有 s3/boto。
上游 PR #1913（remote backend）、#6325（BlobStorageMiddleware）均 closed 未合入。
社区包 `deepagents-backends` 有 S3 实现，但它按「逐次 GET」设计，且我们要复用
`SpaceBackend` 的路径判定（那才是访问控制的单点），所以自己实现一个薄类更省。

## Considered Options

| 方案 | 否决理由 |
|---|---|
| 保留 FilesystemBackend，生产靠同步盘 | 多副本不一致；多一个部署依赖 |
| 引入社区 `deepagents-backends` | 判定单点会裂成两处；依赖面大（`aioboto3`） |
| FUSE 挂载（`rclone mount` / `seaweedfs mount`） | Windows 无 FUSE（需 WinFsp），且把一个可靠的 HTTP 层换成不可靠的内核层 |
| 双实现改成一个实现（彻底删 local） | 本地开发要跑 SeaweedFS 才能看文档，开发体验与测试成本都变差 |

## 后果

**要接受的**：

- 多一个拉取步骤：`s3` 模式下镜像不存在时会在第一次构图时拉一次（阻塞），或显式跑
  `scripts/okf_pull_s3.py`。改了空间内容需要重新拉（或设 `OKF_MIRROR_TTL_SECONDS`）。
- 镜像占本地磁盘（现在 145 K），且**不是**实时同步。
- `has_tables()`（决定装不装 xlsx 四件套）跟着镜像走，无需改代码。

## 怎么验证（本地）

```bash
.venv/Scripts/python.exe scripts/okf_push_s3.py --dry-run
.venv/Scripts/python.exe scripts/okf_push_s3.py
OKF_BACKEND=s3 .venv/Scripts/python.exe -m pytest tests/unit/test_space_backend.py -q
```
