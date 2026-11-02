"""上传入口：`POST /custom/uploads`（ADR-0018）。

一个端点收所有上传，按文件名决定谁处理（`shared.session.upload_rule.route_upload`）：
`<空间名>_xxx.xlsx` → 那个空间的子 agent；其余 xlsx → 通用 `xlsx` 子 agent；
md 等其他文件 → 主 agent 自己读。

前端目前一次都没调过这个接口（`web/` 里 0 处 upload 引用），所以没有兼容负担。
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.auth_deps import require_auth
from aegra_api.core.orm import Thread as ThreadORM
from aegra_api.core.orm import get_session
from aegra_api.models.auth import User
from custom.db.repositories import UserSpaceRepository
from custom.service import uploads

logger = structlog.getLogger(__name__)

router = APIRouter(prefix="/custom", tags=["/custom"])


async def _authorize_thread(session: AsyncSession, thread_id: str, user: User) -> None:
    """上传只能写自己（或服务身份）的会话；不认识的线程直接 404。"""
    owner = await session.scalar(select(ThreadORM.user_id).where(ThreadORM.thread_id == thread_id))
    if owner is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if owner != user.identity:
        raise HTTPException(status_code=403, detail="无权向该会话上传文件")


@router.post("/uploads", status_code=201)
async def upload_file(
    file: Annotated[UploadFile, File(description="要上传的文件；同名即覆盖")],
    thread_id: Annotated[str, Form(description="会话（主管线程）id")],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict:
    """上传一个文件到当前会话；返回它落到了哪个 agent。"""
    await _authorize_thread(session, str(thread_id), user)
    spaces = await UserSpaceRepository(session).list_by_user(user.identity)
    try:
        result = await uploads.save_upload(
            session,
            owner=user.identity,
            thread_id=str(thread_id),
            name=file.filename or "",
            src=file.file,
            spaces=list(spaces),
        )
    except uploads.UploadError as e:
        await session.rollback()
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except ValueError as e:
        # thread_id 拼不进上传路径（含分隔符/保留字符）：4xx，不是 500。
        await session.rollback()
        raise HTTPException(status_code=422, detail=f"当前会话不能存放上传文件：{e}") from e
    await session.commit()
    return result
