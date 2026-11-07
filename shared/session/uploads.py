"""上传文件的介质层：字节落在本地目录（ADR-0018 §9）。

两本账的分工不变：**字节**在 `data/uploads/<owner>/<thread>/<name>`，**归属**在
`session_object` 台账里。本模块只管"字节放哪、怎么取、怎么删"，不管鉴权与配额
（那是 `custom/service/uploads.py`）。

为什么有这一层：上传、派活、表格工具三处都需要"拿到一个真实文件路径"，把路径
计算与读写收在一处，越界校验（`_inside`）才能只写一遍。

`local-spacebackend` 分支只用本地盘：知识读 `knowledge/`，上传落 `data/uploads/`，
派生 parquet 落本机缓存——`s3.endpoint` 留空也能全跑。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO

import structlog

from shared import config
from shared.paths import safe_segment

logger = structlog.getLogger(__name__)

#: 单个路径段的上限。文件名在 `custom/service/uploads.py::safe_name` 另有 200 的字长限制；
#: 这里放到 255，是因为 thread_id 由客户端决定（`ENTITY_ID_PATTERN = r"\S"`，上限 255），
#: 用 200 会让 201–255 长度的合法线程直接报错。
_MAX_SEGMENT = 255

_CHUNK = 1024 * 1024


class UploadTooLarge(Exception):
    """超过单文件上限（映射 413）。"""


class UploadUnavailable(Exception):
    """介质不可用/写失败（映射 503）。"""


# ---------- 路径 ----------


def local_root() -> Path:
    """本地介质根（`uploads.dir`，默认 `data/uploads`）。"""
    return (config.ROOT / str(config.get("uploads.dir") or "data/uploads")).resolve()


def local_dir(owner: str, thread_id: str) -> Path:
    """某个会话的上传目录。不创建——调用方需要时自己 `mkdir`。"""
    return _inside(owner, thread_id)


def local_path(owner: str, thread_id: str, name: str) -> Path:
    """一个上传文件的绝对路径。"""
    return _inside(owner, thread_id, name)


def _inside(*parts: str) -> Path:
    """拼路径并断言没跑出 `local_root()`：读、写、删三个 sink 都从这里过。

    owner / thread / name 都可能来自客户端（注册的 identity、上传表单的 thread_id、
    文件名），任何一段带分隔符或 `..` 都会把路径搬到根外（ADR-0018 §10）。
    """
    root = local_root()
    path = root.joinpath(*(safe_segment(part, limit=_MAX_SEGMENT) for part in parts)).resolve()
    if not path.is_relative_to(root):
        raise ValueError("上传路径越界")
    return path


def key(owner: str, thread_id: str, name: str) -> str:
    """台账里的 `object_key`（本地介质就是相对路径）。"""
    return f"{owner}/{thread_id}/{name}"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------- 写 ----------


def put_stream(
    src: BinaryIO,
    *,
    owner: str,
    thread_id: str,
    name: str,
    max_bytes: int,
) -> tuple[str, str, int]:
    """把文件对象写进本地盘，返回 `(object_key, sha256, size)`。

    先写同目录临时文件再 `os.replace`：读端永远看不到半截文件，崩在中途也只留一个
    `.tmp-*`（reaper 会清）。
    """
    target = local_path(owner, thread_id, name)
    tmp = ensure_dir(target.parent) / f".tmp-{uuid.uuid4().hex}"
    digest = hashlib.sha256()
    total = 0
    limit = f"文件超过 {max_bytes // (1024 * 1024)}MB 上限"
    try:
        try:
            with tmp.open("wb") as out:
                while chunk := src.read(_CHUNK):
                    total += len(chunk)
                    if total > max_bytes:
                        raise UploadTooLarge(limit)
                    digest.update(chunk)
                    out.write(chunk)
        except OSError as e:
            raise UploadUnavailable(f"本地介质写入失败：{e}") from e
        _replace_with_retry(tmp, target)
    except BaseException:
        _unlink_quietly(tmp)
        raise
    return key(owner, thread_id, name), digest.hexdigest(), total


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:  # pragma: no cover - 权限问题时只记日志，别把主流程带崩
        logger.warning("upload_unlink_failed", path=str(path), error=str(e))


def _replace_with_retry(tmp: Path, target: Path, attempts: int = 3) -> None:
    """`os.replace`，Windows 上目标被读句柄占着时重试几次（读端通常很快释放）。"""
    for left in range(attempts, 0, -1):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if left == 1:
                raise OSError("目标文件正被读取，请重试") from None
            time.sleep(0.05)


# ---------- 读 ----------


def materialize(owner: str, thread_id: str, name: str) -> Path | None:
    """返回文件的本地路径；不存在返回 None（本地介质不需要"拉取"这一步）。"""
    target = local_path(owner, thread_id, name)
    return target if target.is_file() else None


def read_bytes(owner: str, thread_id: str, name: str) -> bytes | None:
    path = materialize(owner, thread_id, name)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


# ---------- 覆盖前备份 ----------

_BACKUP_PREFIX = ".bak-"


def backup(owner: str, thread_id: str, name: str) -> str | None:
    """覆盖上传前把旧字节挪到备份位，返回恢复凭据；没有旧字节返回 None。

    同目录 rename：原子、不额外占空间，`_replace_with_retry` 会处理句柄占用。
    """
    token = f"{_BACKUP_PREFIX}{uuid.uuid4().hex}"
    target = local_path(owner, thread_id, name)
    if not target.is_file():
        return None
    try:
        _replace_with_retry(target, target.parent / token)
    except OSError as e:
        logger.warning("upload_backup_failed", name=name, error=str(e))
        return None
    return token


def restore(owner: str, thread_id: str, name: str, token: str) -> None:
    """把备份换回原名（覆盖失败时回滚）。"""
    bak = local_path(owner, thread_id, token)
    if bak.is_file():
        _replace_with_retry(bak, local_path(owner, thread_id, name))


def drop_backup(owner: str, thread_id: str, token: str) -> None:
    """台账已经落库，备份可以丢了（尽力而为，失败让 reaper 清）。"""
    _unlink_quietly(local_path(owner, thread_id, token))


# ---------- 删 ----------


def delete(owner: str, thread_id: str, name: str) -> None:
    """删掉一个上传文件（失败只记日志）。"""
    _unlink_quietly(local_path(owner, thread_id, name))


def delete_thread(owner: str, thread_id: str) -> None:
    """删掉一个会话的上传目录。"""
    shutil.rmtree(local_dir(owner, thread_id), ignore_errors=True)


# ---------- 对账（reaper 用） ----------


def iter_local_keys() -> Any:
    """遍历本地介质里所有上传文件，产出 `(owner, thread, name, path)`。"""
    root = local_root()
    if not root.is_dir():
        return
    for owner_dir in root.iterdir():
        if not owner_dir.is_dir():
            continue
        for thread_dir in owner_dir.iterdir():
            if not thread_dir.is_dir():
                continue
            for path in thread_dir.iterdir():
                if path.is_file() and not path.name.startswith((".tmp-", _BACKUP_PREFIX)):
                    yield owner_dir.name, thread_dir.name, path.name, path
