"""上传：字节进对象存储，元数据进 `session_object`（ADR-0020）。**本函数自己 commit**。

两本账的分工：对象存储是真相（key = ``uploads/<owner>/<thread>/<文件名>``），
`session_object` 行回答"这个会话里有哪些文件、该派给谁"。**读路径不查这张表**
（Mirage 直接按前缀列对象），只有上传写入、配额、删会话清理会用到它。

写序不可换（与旧 ADR-0013 同一条教训）：

1. 把字节写进对象存储（``blobs.put_stream``，边写边算 sha256，超限自己 abort）；
2. 表格在上传期同步转一次派生（PRD 5.6.4）：转不动就是上传失败，字节回滚；
3. 再 upsert 台账行 —— 行里带上传那一刻算好的投递目标 ``target_agent``。

崩在 1 和 3 之间的后果：多一个没人认领的对象（下次同名上传按覆盖处理），
比"行说有、字节没有"安全得多；反过来则会出现台账指向不存在的对象。
所以 2/3 失败时尽力把 1 写的对象删掉，让状态回到"两边都没有"。
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import uuid
from pathlib import Path
from typing import Any, BinaryIO

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from custom.db.models import SessionObject
from shared import config
from shared.paths import safe_segment
from shared.session import blobs
from shared.session.upload_rule import is_xlsx, route_upload

logger = structlog.getLogger(__name__)

#: 文件名里出现就拒绝的字符：路径分隔符与 NUL。**不做**"清洗成合法名"——
#: 上传要按用户看到的文件名落到 /uploads 下，改名会让 agent 找不到。
_BAD_CHARS = ("/", "\\", "\x00")
_MAX_NAME = 200

#: 本服务自己的临时/备份前缀：`.bak-` 是覆盖前的旧字节（同名上传的备份位）。
#: 这类名字留给服务内部，用户占用会和备份撞名。
_TEMP_PREFIXES = (".tmp-", ".bak-")


class UploadError(Exception):
    """能直接翻成 4xx 的上传错误。"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def safe_name(name: str) -> str:
    """校验上传文件名（保留原名，不做清洗）。"""
    name = (name or "").strip()
    if name.lower().startswith(_TEMP_PREFIXES):
        raise UploadError("文件名不合法：前缀与本服务的临时文件冲突（.tmp- / .bak-），请改名", 422)
    try:
        return safe_segment(name, limit=_MAX_NAME)
    except ValueError as e:
        raise UploadError(f"文件名不合法：{e}", 422) from e


def size_limit(name: str) -> int:
    """单文件字节上限：表格按 `xlsx.max_blob_mb`，其他按 `session_files.max_upload_mb`。"""
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


async def _convert_table(sha256: str, key: str) -> list[dict[str, Any]]:
    """上传期同步转一次派生缓存（PRD 5.6.4）：失败就当上传失败。

    同内容再传（含改名）时清单已命中，直接复用——派生按内容指纹寻址，不按文件。
    """
    # 唯一一处 custom -> graphs：`duck` 只用 shared + duckdb/openpyxl 的惰导入，
    # 不会把整张图拖进 HTTP 启动路径（白名单见 tests/unit/test_layout.py）。
    from graphs.tools.xlsx import duck

    existing = await asyncio.to_thread(duck.reload_manifest, sha256, 0)
    if existing is not None:
        return list(existing["sheets"])
    timeout = float(config.get("xlsx.convert_timeout_seconds"))
    data = await asyncio.to_thread(blobs.get_or_none, key)
    if data is None:
        raise UploadError("对象存储读取失败，请重试", 503)
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "upload.xlsx"
        source.write_bytes(data)
        try:
            manifest = await asyncio.wait_for(
                asyncio.to_thread(duck.convert_upload, sha256, source, 0), timeout
            )
        except TimeoutError as exc:
            # shortcut: 线程池里那次转换还在跑，可能事后又写一份派生；反正这份东西
            # 下次查询会重算，多出来的字节由 xlsx.cache_local_max_mb 淘汰。
            with contextlib.suppress(Exception):
                await asyncio.to_thread(duck.drop_derived, sha256)
            raise UploadError(f"表格转换超时（{timeout:g} 秒），请拆分后重传", 422) from exc
        except duck.TableError as exc:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(duck.drop_derived, sha256)
            raise UploadError(str(exc), 422) from exc
    return list(manifest["sheets"])


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
    key = blobs.upload_key(owner, thread_id, name)
    limit = size_limit(name)

    # 会话级互斥：锁住 thread 行，直到本事务 commit。
    # ① 同一会话的上传串行，配额计数才准（#13）；② run 创建路径里 set_thread_status
    # 的 UPDATE 会在这把行锁上排队 —— 也就是说"发消息"不会插进上传的中间。
    await session.execute(
        text("SELECT thread_id FROM thread WHERE thread_id = :thread_id FOR UPDATE"),
        {"thread_id": thread_id},
    )

    # 配额：计数与插入现在都在上面那把行锁里，同一会话不会并发越过上限。
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

    # 覆盖同名文件会把旧字节冲掉：先复制一份备份，落库/提交失败再换回来（#7）。
    backup: str | None = None
    if await asyncio.to_thread(blobs.stat, key) is not None:
        backup = f"{blobs.upload_prefix(owner, thread_id)}.bak-{uuid.uuid4().hex}"
        # 备份失败就直接抛：没有旧字节的副本，覆盖是不安全的。
        await asyncio.to_thread(blobs.copy, key, backup)

    async def _revert() -> None:
        with contextlib.suppress(Exception):
            if backup is None:
                await asyncio.to_thread(blobs.delete, key)
            else:
                await asyncio.to_thread(blobs.copy, backup, key)
                await asyncio.to_thread(blobs.delete, backup)

    try:
        sha256, size = await asyncio.to_thread(blobs.put_stream, src, key, max_bytes=limit)
    except blobs.BlobTooLarge as e:
        raise UploadError(str(e), 413) from e
    except blobs.BlobUnavailable as e:
        raise UploadError("对象存储暂不可用，请稍后重试", 503) from e

    target = route_upload(name, spaces)
    space = next((s for s in spaces if target == s), "")
    # 表格在上传阶段就把派生建完：这一步失败 = 上传失败（原件回滚，不落台账）
    sheets: list[dict[str, Any]] = []
    if is_xlsx(name):
        try:
            sheets = await _convert_table(sha256, key)
        except BaseException:
            await _revert()
            raise
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
            # Core upsert 不会触发 `onupdate=func.now()`，必须显式写（#13）
            "updated_at": func.now(),
        },
    )
    try:
        await session.execute(stmt)
        await session.commit()
    except BaseException:
        await _revert()
        raise
    if backup is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(blobs.delete, backup)

    logger.info(
        "upload_saved",
        owner=owner,
        thread_id=thread_id,
        name=name,
        size=size,
        target_agent=target,
    )
    return {
        "name": name,
        "size": size,
        "sha256": sha256,
        "target_agent": target,
        "virtual_path": f"/uploads/{name}",
        "sheets": [{"name": s["name"], "rows": s["rows"], "columns": s["columns"]} for s in sheets],
    }
