"""上传入口：`POST /custom/uploads`（ADR-0020）。

一次收**多个**文件，按文件名决定谁处理（`shared.session.upload_rule.route_upload`）：
`<空间名>_xxx.xlsx` → 那个空间的子 agent；其余 xlsx → 通用 `xlsx` 子 agent；
md 等其他文件 → 主 agent 自己读。

每个文件独立成败：全成功 201，部分失败 207（body 里逐个给原因），整批超总字节上限
413 且不写任何东西（PRD 5.5.4）。表格的转换也在 `save_upload` 里同步完成，失败只
影响它自己。
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.auth_deps import require_auth
from aegra_api.core.orm import Thread as ThreadORM
from aegra_api.core.orm import get_session
from aegra_api.models.auth import User
from custom.db.repositories import UserSpaceRepository
from custom.service import uploads
from shared import config

logger = structlog.getLogger(__name__)

router = APIRouter(prefix="/custom", tags=["/custom"])


async def _authorize_thread(session: AsyncSession, thread_id: str, user: User) -> None:
    """上传只能写自己（或服务身份）的会话；不认识的线程直接 404。"""
    owner = await session.scalar(select(ThreadORM.user_id).where(ThreadORM.thread_id == thread_id))
    if owner is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if owner != user.identity:
        raise HTTPException(status_code=403, detail="无权向该会话上传文件")


@router.post("/uploads")
async def upload_files(
    response: Response,
    files: Annotated[list[UploadFile], File(description="要上传的文件，可一次多个；同名即覆盖")],
    thread_id: Annotated[str, Form(description="会话（主管线程）id")],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict:
    """上传一批文件到当前会话；返回每个文件落到了哪个 agent 以及表格结构。"""
    await _authorize_thread(session, str(thread_id), user)
    # 整批预检：超了直接拒绝，一个字节都不写（PRD 5.5.4「超额拒绝且无残留」）。
    batch_limit = int(config.get("session_files.max_batch_mb")) * 1024 * 1024
    total = sum(int(f.size or 0) for f in files)
    if total > batch_limit:
        raise HTTPException(
            status_code=413, detail=f"一次上传总大小超过 {batch_limit // (1024 * 1024)}MB 上限"
        )
    spaces = list(await UserSpaceRepository(session).list_by_user(user.identity))

    results: list[dict] = []
    for file in files:
        name = file.filename or ""
        try:
            saved = await uploads.save_upload(
                session,
                owner=user.identity,
                thread_id=str(thread_id),
                name=name,
                src=file.file,
                spaces=spaces,
            )
            results.append({"status": "ok", **saved})
        except uploads.UploadError as e:
            await session.rollback()  # 失败那个自己回滚，已成功的照旧留在库里
            results.append({"name": name, "status": "failed", "code": e.status_code, "error": e.message})
        except ValueError as e:
            await session.rollback()
            results.append({"name": name, "status": "failed", "code": 422, "error": f"上传参数不合法：{e}"})
        finally:
            await file.close()

    if any(item["status"] != "ok" for item in results):
        response.status_code = 207
    else:
        response.status_code = 201
    return {"files": results}
