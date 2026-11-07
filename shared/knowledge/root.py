"""知识空间根：直接读仓库根的 `knowledge/` 目录（ADR-0016）。

`local-spacebackend` 只支持本地知识：`okf.backend` 开关已删，进程读什么就是盘上什么。
`push()` 保留给 `scripts/okf_push_s3.py`（把知识同步到对象存储供其他产品线用），
读路径不再有镜像/拉取这一步。
"""

from __future__ import annotations

from pathlib import Path

from shared import config
from shared.session import blobs

PROJECT_ROOT = config.ROOT
_PREFIX = "okf/"


def local_root() -> Path:
    """仓库里自带的知识空间根。

    数据不在代码包里：`knowledge/` 是仓库根的顶层目录，不是 `graphs/` 的子目录。
    """
    return (PROJECT_ROOT / config.get("okf.dir")).resolve()


def knowledge_root() -> Path:
    """当前生效的知识空间根（本地线只有这一种）。"""
    return local_root()


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
