"""知识空间根：`okf.backend: local` 直接读盘，`: s3` 读 S3 的本地镜像（ADR-0016）。

为什么是「镜像」而不是手写一个 S3 backend：deepagents 的 `BackendProtocol` 有 14 个方法
（ls / read / grep / glob / write / edit / delete / upload / download + 各自的 async 版），
而 `ls` 的 virtual_mode 语义、`grep` 的 glob 契约、`read` 的分页与二进制判定都很细。
把它们在对象存储上重写一遍，正确性风险远大于收益，且任何偏差都会**静默**改变 agent 行为。

镜像换来的性质一样：空间内容存在 S3（多副本看到同一份），进程只读本地副本，
8 个 fs 工具走的还是同一份经过测试的实现。

刷新时机：镜像缺失时拉一次；`okf.mirror_ttl_seconds`（默认 0 = 不自动刷新）到期后再拉。
改了空间内容就重跑 `scripts/okf_pull_s3.py`，或把 TTL 设成几百秒让它自己过期。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from shared import config
from shared.session import blobs

PROJECT_ROOT = config.ROOT
_PREFIX = "okf/"
_mirror_lock = threading.Lock()
_last_pull: float = 0.0


def local_root() -> Path:
    """仓库里自带的知识空间根（`local` 模式的真相，`s3` 模式下的上传源）。

    数据不在代码包里：`knowledge/` 是仓库根的顶层目录，不是 `graphs/` 的子目录。
    """
    return (PROJECT_ROOT / config.get("okf.dir")).resolve()


def mirror_root() -> Path:
    """`s3` 模式下的本地镜像目录；默认 `.cache/okf`（必须在 OKF 根之外）。"""
    return Path(config.get("okf.mirror_dir")).resolve()


def _ttl() -> float:
    return float(config.get("okf.mirror_ttl_seconds"))


def knowledge_root(*, force_pull: bool = False) -> Path:
    """当前生效的知识空间根。`local` 模式就是仓库根的 `knowledge/`。

    `s3` 模式：镜像不存在或已过 TTL 时拉一次。拉取失败**不**静默降级到本地盘——
    降级会让不同副本读到不同内容，比直接报错更难查（调用方会看到明确异常）。
    """
    if config.backend_mode() != "s3":
        return local_root()
    target = mirror_root()
    if force_pull or not target.is_dir() or _expired():
        pull(target)
    return target


def _expired() -> bool:
    ttl = _ttl()
    return ttl > 0 and (time.time() - _last_pull) > ttl


def pull(target: Path | None = None) -> int:
    """把 S3 上的 `okf/` 前缀同步到本地镜像，返回下载的文件数（幂等）。"""
    global _last_pull
    dest = target or mirror_root()
    with _mirror_lock:
        s3 = blobs.client()
        bucket = blobs.bucket()
        dest.mkdir(parents=True, exist_ok=True)
        written = 0
        token: str | None = None
        while True:
            kw = {"Bucket": bucket, "Prefix": _PREFIX}
            if token:
                kw["ContinuationToken"] = token
            page = s3.list_objects_v2(**kw)
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(_PREFIX) :]
                if not rel or rel.endswith("/"):
                    continue
                out = dest / rel
                if out.is_file() and out.stat().st_size == obj.get("Size"):
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                tmp = out.parent / (out.name + ".tmp")
                blobs.download(obj["Key"], str(tmp))
                os.replace(tmp, out)
                written += 1
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        _last_pull = time.time()
        return written


def push(source: Path | None = None, *, prune: bool = False) -> tuple[int, int]:
    """把本地 `knowledge` 推到 S3 的 `okf/` 前缀，返回 (上传, 删除)。

    幂等：对象已存在且大小相同就跳过。`prune=True` 时顺带删掉 S3 上本地已没有的对象。
    """
    src = (source or local_root()).resolve()
    s3 = blobs.client()
    bucket = blobs.bucket()
    existing: dict[str, int] = {}
    token: str | None = None
    while True:
        kw = {"Bucket": bucket, "Prefix": _PREFIX}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            existing[obj["Key"]] = obj.get("Size", -1)
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")

    pushed = 0
    seen: set[str] = set()
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.name.endswith(".tmp"):
            continue
        rel = path.relative_to(src).as_posix()
        key = f"{_PREFIX}{rel}"
        seen.add(key)
        size = path.stat().st_size
        if existing.get(key) == size:
            continue
        blobs.client().upload_file(str(path), bucket, key)
        pushed += 1

    pruned = 0
    if prune:
        stale = [k for k in existing if k not in seen]
        for key in stale:
            blobs.delete(key)
            pruned += 1
    return pushed, pruned
