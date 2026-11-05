"""上传：字节进介质（本地目录或对象存储），元数据进 `session_object`（ADR-0018）。

两本账的分工：介质是真相（key/路径 = ``<owner>/<thread>/<文件名>``），
`session_object` 行回答"这个会话里有哪些文件、该派给谁"。**读路径不查这张表**
（本地目录直接列，s3fs 由 Mirage 按前缀列对象），只有上传写入、配额、删会话清理会用到它。

介质的选择（`uploads.store`）收在 `shared/session/uploads.py`：这里不判断是 S3 还是
本地目录，只调 ``put_stream`` / ``delete``，所以单机部署改一个配置值就够。

写序不可换（与旧 ADR-0013 同一条教训）：

1. 把字节写进介质（``uploads.put_stream``，边写边算 sha256，超限自己清干净）；
2. 再 upsert 台账行 —— 行里带上传那一刻算好的投递目标 ``target_agent``。

崩在 1 和 2 之间的后果：多一个没人认领的文件（下次同名上传按覆盖处理），
比"行说有、字节没有"安全得多；反过来则会出现台账指向不存在的文件。
所以覆盖上传时先把旧字节挪到备份位（`uploads.backup`），台账/提交失败就把旧字节
换回来；新建（没有旧字节）失败才把刚写的字节删掉。这样"旧文件被冲掉、新文件也没
落库"这个组合不会出现（issue #7）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from typing import Any, BinaryIO

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from custom.db.models import SessionObject
from shared import config
from shared.paths import safe_segment
from shared.session import uploads as upload_store
from shared.session.upload_rule import route_upload

logger = structlog.getLogger(__name__)

_MAX_NAME = 200


class UploadError(Exception):
    """能直接翻成 4xx 的上传错误。"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def safe_name(name: str) -> str:
    """校验上传文件名（保留原名，不做清洗）。

    与 `shared.paths.safe_segment` 同一套判据：名字要原样拼进上传路径，
    所以分隔符、`:`（NTFS 数据流）、以点结尾（Windows 静默改写）都不收。
    """
    name = (name or "").strip()
    try:
        return safe_segment(name, limit=_MAX_NAME)
    except ValueError as e:
        raise UploadError(f"文件名不合法：{e}", 422) from e


def size_limit(name: str) -> int:
    """单文件字节上限：表格按 `xlsx.max_blob_mb`，其他按 `session_files.max_upload_mb`。"""
    from shared.session.upload_rule import is_xlsx

    mb = config.get("xlsx.max_blob_mb" if is_xlsx(name) else "session_files.max_upload_mb")
    return int(mb) * 1024 * 1024


async def count_files(session: AsyncSession, owner: str, thread_id: str) -> int:
    """该会话已有的上传文件数（配额用）。"""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(SessionObject)
            .where(SessionObject.owner_identity == owner, SessionObject.thread_id == thread_id)
        )
        or 0
    )


def _lock_key(owner: str, thread_id: str, name: str) -> int:
    """同名上传的咨询锁键：blake2b 取 8 字节（PG 的 `bigint`，带符号）。"""
    raw = f"{owner}\x00{thread_id}\x00{name}".encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big", signed=True)


async def save_upload(
    session: AsyncSession,
    *,
    owner: str,
    thread_id: str,
    name: str,
    src: BinaryIO,
    spaces: list[str],
) -> dict[str, Any]:
    """写一个上传文件，返回给 HTTP 层的摘要。调用方负责 commit。

    `spaces` 是**上传者当下**的空间集，只用来算 `target_agent`；它不参与鉴权
    （鉴权由路由层的 `_authorize_thread` + 空间成员表决定）。
    """
    name = safe_name(name)
    limit = size_limit(name)

    # 同名并发上传必须串行：备份→覆盖→落库三步之间没有隔离，两个请求会互相冲掉备份，
    # 最后一个赢、另一个的字节凭空消失（#16）。事务级咨询锁在 commit/rollback 时自动释放。
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(owner, thread_id, name)})

    # 配额：先数后插不是原子的，不同名的并发请求仍可能超出 1~2 个。
    # 这是软限制（真正挡人的是单文件大小），不值得为此加行锁。
    max_files = int(config.get("session_files.max_files_per_session"))
    existing = await session.scalar(
        select(SessionObject.sha256).where(
            SessionObject.owner_identity == owner,
            SessionObject.thread_id == thread_id,
            SessionObject.name == name,
        )
    )
    if existing is None and await count_files(session, owner, thread_id) >= max_files:
        raise UploadError(f"每个会话最多 {max_files} 个上传文件", 409)

    # 覆盖同名文件会把旧字节冲掉：先挪到备份位，落库失败再换回来（#7）。
    backup = await asyncio.to_thread(upload_store.backup, owner, thread_id, name)

    async def _revert() -> None:
        with contextlib.suppress(Exception):
            if backup is None:
                await asyncio.to_thread(upload_store.delete, owner, thread_id, name)
            else:
                await asyncio.to_thread(upload_store.restore, owner, thread_id, name, backup)

    try:
        try:
            key, sha256, size = await asyncio.to_thread(
                upload_store.put_stream,
                src,
                owner=owner,
                thread_id=thread_id,
                name=name,
                max_bytes=limit,
            )
        except upload_store.UploadTooLarge as e:
            raise UploadError(str(e), 413) from e
        except upload_store.UploadUnavailable as e:
            raise UploadError("上传存储暂不可用，请稍后重试", 503) from e

        target = route_upload(name, spaces)
        space = next((s for s in spaces if target == s), "")
        stmt = pg_insert(SessionObject).values(
            owner_identity=owner,
            thread_id=thread_id,
            name=name,
            space=space,
            target_agent=target,
            object_key=key,
            sha256=sha256,
            size_bytes=size,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["owner_identity", "thread_id", "name"],
            set_={
                "space": space,
                "target_agent": target,
                "object_key": key,
                "sha256": sha256,
                "size_bytes": size,
            },
        )
        await session.execute(stmt)
        # 提交也在 try 里：commit 失败时台账没落库，必须把旧字节换回来（#7）
        await session.commit()
    except BaseException:
        await _revert()
        raise
    if backup is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(upload_store.drop_backup, owner, thread_id, backup)

    logger.info(
        "upload_saved",
        owner=owner,
        thread_id=thread_id,
        name=name,
        size=size,
        target_agent=target,
        store=upload_store.store_mode(),
    )
    return {
        "name": name,
        "size": size,
        "sha256": sha256,
        "target_agent": target,
        "virtual_path": f"/uploads/{name}",
    }
