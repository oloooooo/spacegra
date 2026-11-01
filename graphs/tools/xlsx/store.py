"""虚拟路径 → 本地可读的 xlsx + 索引缓存键（ADR-0015 §4）。

`/空间名/x.xlsx`：知识空间里的存量文件，走本地盘（`graphs/<okf.dir>`），只读。

`xlsx.cache_dir` 不能落在 OKF 根下——根散文件对所有登录用户只读共享。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from shared import config


def cache_dir() -> Path:
    return Path(config.get("xlsx.cache_dir")).resolve()


def virtual_cache_path(vpath: str, data: bytes) -> Path:
    """s3fs 模式：把虚拟路径的字节落到本地缓存，返回本地路径。

    文件名取内容哈希：同一张表重复下载不会让索引失效（`materialize` 的缓存键是
    「路径 + mtime + size」），表改了才会换一个新文件名。
    """
    digest = hashlib.sha256(data).hexdigest()[:16]
    target = cache_dir() / "vpath" / f"{digest}{Path(vpath).suffix or '.bin'}"
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return target


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
