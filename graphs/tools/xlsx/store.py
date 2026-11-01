"""虚拟路径 → 本地可读的 xlsx + 索引缓存键（ADR-0015 §4）。

`/空间名/x.xlsx`：知识空间里的存量文件，走本地盘（`graphs/<okf.dir>`），只读。

`xlsx.cache_dir` 不能落在 OKF 根下——根散文件对所有登录用户只读共享。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

from shared import config

_REPLACE_ATTEMPTS = 6


def cache_dir() -> Path:
    return Path(config.get("xlsx.cache_dir")).resolve()


def write_atomic(target: Path, data: bytes) -> Path:
    """同目录临时文件 + rename 落盘（并发安全）。

    缓存文件名由内容哈希决定，多个 worker 同时写同一个 key 时字节完全相同，但：
    - 直接 `write_bytes` 会让**读者看到半截文件**；
    - Windows 上 `os.replace` 在两个进程同时替换同一目标时会回 `PermissionError`
      （Linux 上 POSIX rename 原子且不会失败）。

    所以：先写临时文件，再 replace；replace 失败时先看目标是否已经被别的 worker
    写完（大小一致就接受，内容按哈希保证相同），否则小退避重试。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, target)
                return target
            except PermissionError:
                if _same_size(target, data):
                    return target
                time.sleep(0.01 * (attempt + 1))
        raise OSError(f"无法落盘缓存文件（并发写入冲突）: {target}")
    finally:
        Path(tmp).unlink(missing_ok=True)


def _same_size(path: Path, data: bytes) -> bool:
    try:
        return path.stat().st_size == len(data)
    except OSError:
        return False


def virtual_cache_path(vpath: str, data: bytes) -> Path:
    """s3fs 模式：把虚拟路径的字节落到本地缓存，返回本地路径。

    文件名取内容哈希：同一张表重复下载不会让索引失效（`materialize` 的缓存键是
    「路径 + mtime + size」），表改了才会换一个新文件名。
    """
    digest = hashlib.sha256(data).hexdigest()[:16]
    target = cache_dir() / "vpath" / f"{digest}{Path(vpath).suffix or '.bin'}"
    if target.is_file():
        return target
    return write_atomic(target, data)


def root() -> Path:
    """与 backend 同一根：虚拟路径 /a.xlsx 落到 <okf>/a.xlsx（s3 模式下是本地镜像）。"""
    from shared.knowledge.root import knowledge_root

    return knowledge_root()


def resolve_local(path: str | Path) -> Path:
    """绝对路径当虚拟路径：/kb/a.xlsx、/a.xlsx 都相对 OKF 根解析，不允许越界。"""
    raw = str(path).strip()
    p = Path(raw)
    if raw.startswith(("/", "\\")):
        root_dir = root()
        p = (root_dir / raw.lstrip("/\\")).resolve()
        if root_dir not in p.parents:
            raise ValueError(f"路径越界：{path}")
    # 相对路径分支（如 knowledge/消融实验1.xlsx）不查越界——那是调用方自己的 cwd
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在：{p}")
    return p


def display_name(path: str | Path) -> str:
    return str(path).rstrip("/\\").rsplit("/", 1)[-1]


def materialize(path: str | Path) -> tuple[Path, str]:
    """返回 (本地 xlsx, 索引缓存键)。"""
    local = resolve_local(path)
    stat = local.stat()
    # 空间文件可能被手工改过：mtime+size 一起进键，改了就换索引
    return local, f"local-{local.parent.name}-{local.name}-{int(stat.st_mtime)}-{stat.st_size}"
