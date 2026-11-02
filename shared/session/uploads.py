"""上传文件的介质层：本地目录或对象存储（ADR-0018 §9）。

两本账的分工不变：**字节**落在介质里，**归属**在 `session_object` 台账里。本模块只
负责"字节放哪、怎么取、怎么删"，不管鉴权与配额（那是 `custom/service/uploads.py`）。

为什么要这一层：单机部署没有对象存储，但上传/派活/表格工具三处都假定"字节在 S3"。
把介质判断收进一个模块，业务侧只调 `put_stream` / `materialize` / `delete`，换介质
就是换一个配置值（`uploads.store`）。

三种组合（`okf.backend` × `uploads.store`）：

===============  ==============  ==================================================
okf.backend      uploads.store   读上传文件的路径
===============  ==============  ==================================================
``local``        ``fs``           `data/uploads/...` 直接读（LocalBackend 挂 /uploads）
``s3``（镜像）    ``s3``          按需把对象拉到 `data/uploads/...` 再读
``s3fs``          ``s3``          Mirage 的 S3VFS 直读，**不经过本模块**
===============  ==============  ==================================================

`uploads.store` 缺省 `auto`：配了对象存储（`s3.endpoint` 非空）就用 s3，否则用 fs。
`s3fs` 模式要求 s3（没有本地目录可读），启动模型时在校验里报错。

key 的形状两种介质刻意保持同构：``<owner>/<thread>/<name>``。对象存储那边加
``uploads/`` 前缀（`blobs.upload_prefix`），本地这边加 `data/uploads/` 目录前缀。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, BinaryIO

import structlog

from shared import config
from shared.paths import safe_segment
from shared.session import blobs

logger = structlog.getLogger(__name__)

_CHUNK = 1024 * 1024


class UploadTooLarge(Exception):
    """超过单文件上限（映射 413）。"""


class UploadUnavailable(Exception):
    """介质不可用/写失败（映射 503）。"""


# ---------- 介质与路径 ----------


def store_mode() -> str:
    """``fs`` 或 ``s3``；`auto` 按是否配了对象存储决定。"""
    raw = str(config.get("uploads.store") or "auto").strip().lower()
    if raw in {"fs", "s3"}:
        return raw
    return "s3" if blobs.configured() else "fs"


def local_root() -> Path:
    """本地介质根（`uploads.dir`，默认 `data/uploads`）。"""
    return (config.ROOT / str(config.get("uploads.dir") or "data/uploads")).resolve()


def local_dir(owner: str, thread_id: str) -> Path:
    """某个会话的上传目录。不创建——调用方需要时自己 `mkdir`。"""
    return _inside(owner, thread_id)


def local_path(owner: str, thread_id: str, name: str) -> Path:
    """一个上传文件在本地介质里的绝对路径（不看介质类型，只算位置）。"""
    return _inside(owner, thread_id, name)


def _inside(*parts: str) -> Path:
    """拼路径并断言没跑出 `local_root()`：读、写、删三个 sink 都从这里过。

    owner / thread / name 都可能来自客户端（注册的 identity、上传表单的 thread_id、
    文件名），任何一段带分隔符或 `..` 都会把路径搬到根外（ADR-0018 §10）。
    """
    root = local_root()
    path = root.joinpath(*(safe_segment(part) for part in parts)).resolve()
    if not path.is_relative_to(root):
        raise ValueError("上传路径越界")
    return path


def key(owner: str, thread_id: str, name: str) -> str:
    """台账里的 `object_key`：两种介质同构，方便切换后对账。"""
    if store_mode() == "s3":
        return blobs.upload_key(owner, thread_id, name)
    return f"{owner}/{thread_id}/{name}"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate() -> None:
    """启动/建模时的一致性检查。

    `s3fs` 模式下读取靠 Mirage 的 S3VFS（挂的是对象存储的 `uploads/<owner>/<thread>`
    前缀），本地目录里的字节它看不见。所以这种组合一旦配错，读上传文件会“静默空目录”，
    很难排查——直接在建模时抬错，把配置问题暴露在明面。
    """
    if str(config.get("okf.backend")).strip().lower() != "s3fs":
        return
    if store_mode() == "s3":
        return
    raise RuntimeError(
        "okf.backend=s3fs 要求上传落对象存储：把 uploads.store 设成 s3，"
        "或配置 s3.endpoint（uploads.store=auto 会自动选 s3）。"
        "Mirage 只读对象存储里的 uploads/ 前缀，看不到本地目录。"
    )


# ---------- 写 ----------


def put_stream(
    src: BinaryIO,
    *,
    owner: str,
    thread_id: str,
    name: str,
    max_bytes: int,
) -> tuple[str, str, int]:
    """把文件对象写进介质，返回 `(object_key, sha256, size)`。

    本地介质先写同目录临时文件再 `os.replace`：读端永远看不到半截文件，崩在中途
    也只留一个 `.tmp-*`（reaper 会清）。对象存储沿用 `blobs.put_stream` 的分片逻辑。
    """
    if store_mode() == "s3":
        try:
            sha256, size = blobs.put_stream(src, blobs.upload_key(owner, thread_id, name), max_bytes=max_bytes)
        except blobs.BlobTooLarge as e:
            raise UploadTooLarge(str(e)) from e
        except blobs.BlobUnavailable as e:
            raise UploadUnavailable(str(e)) from e
        # 覆盖同名文件后本地可能还留着旧的副本，删掉让下次读重新取
        _unlink_quietly(local_path(owner, thread_id, name))
        return key(owner, thread_id, name), sha256, size

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
        os.replace(tmp, target)
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


# ---------- 读 ----------


def materialize(owner: str, thread_id: str, name: str) -> Path | None:
    """确保字节在本地目录里，返回它的路径；取不到返回 None。

    `fs` 介质：文件本来就在本地，存在即返回。
    `s3` 介质：本地没有就从对象存储拉一份（同名覆盖时上传侧会先删本地副本）。
    """
    target = local_path(owner, thread_id, name)
    if target.is_file():
        return target
    if store_mode() != "s3":
        return None
    try:
        data = blobs.get_or_none(blobs.upload_key(owner, thread_id, name))
    except Exception as e:  # noqa: BLE001 - 介质故障不该让读路径抛栈
        logger.warning("upload_materialize_failed", name=name, error=str(e))
        return None
    if data is None:
        return None
    tmp = ensure_dir(target.parent) / f".tmp-{uuid.uuid4().hex}"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError as e:  # pragma: no cover - 磁盘满/权限
        logger.warning("upload_materialize_write_failed", path=str(target), error=str(e))
        _unlink_quietly(tmp)
        return None
    return target


def read_bytes(owner: str, thread_id: str, name: str) -> bytes | None:
    path = materialize(owner, thread_id, name)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


# ---------- 删 ----------


def delete(owner: str, thread_id: str, name: str) -> None:
    """删掉一个上传文件（两种介质一起清）。失败只记日志。"""
    _unlink_quietly(local_path(owner, thread_id, name))
    if store_mode() == "s3":
        try:
            blobs.delete(blobs.upload_key(owner, thread_id, name))
        except Exception as e:  # noqa: BLE001 - 删会话是尽力而为
            logger.warning("upload_delete_failed", name=name, error=str(e))


def delete_thread(owner: str, thread_id: str) -> None:
    """删掉一个会话的本地上传目录（对象存储在 s3fs 模式由 Mirage/前缀清理负责）。"""
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
                if path.is_file() and not path.name.startswith(".tmp-"):
                    yield owner_dir.name, thread_dir.name, path.name, path
