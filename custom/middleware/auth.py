import asyncio
import contextlib
import os

import jwt
import structlog
from langgraph_sdk import Auth
from langgraph_sdk.auth.exceptions import HTTPException as AuthHTTPException
from sqlalchemy import select

from aegra_api.core.database import db_manager
from aegra_api.core.orm import Thread as ThreadORM
from aegra_api.core.orm import get_session_maker
from custom.db.models import SessionObject
from custom.db.repositories import ThreadQuotaRepository, UserRepository, UserSpaceRepository
from custom.service.quota import MAX_THREADS_PER_USER

logger = structlog.getLogger(__name__)

# 内部服务账号（async subagent 的静态 token）：跳过 DB 查询直接放行。
# subagent 拿不到终端用户的 JWT，只能用构建期铸的静态 token，见 graphs/supervisor.py。
from shared.token import SERVICE_IDENTITY

auth = Auth()

@auth.authenticate
async def _decode_access_token(headers: dict[str, str]):
    """
    aegra 中间件按位置传入整个 headers 字典（不是按名注入 token），
    这里自己从 Authorization: Bearer <token> 里取 token。
    """
    scheme, _, token = (headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthHTTPException(headers={"WWW-Authenticate": "Bearer"})
    maker = get_session_maker()
    async with maker() as session:
        user_repo = UserRepository(session=session)
        user_space_repo = UserSpaceRepository(session=session)
        try:
            payload = jwt.decode(
                token,
                os.environ["JWT_SECRET"],
                algorithms=["HS256"],  # 必须显式指定，避免 alg 混淆攻击
                options={"require": ["sub", "iat", "exp"]},
                leeway=5,  # 可按需给时钟偏移留几秒，例如 leeway=5
            )
        except jwt.InvalidTokenError:
            raise AuthHTTPException(
                detail="无效或已过期的令牌", headers={"WWW-Authenticate": "Bearer"}
            )
        if payload.get("type") or not payload.get("sub"):
            raise AuthHTTPException(
                detail="无效或已过期的令牌", headers={"WWW-Authenticate": "Bearer"}
            )
        identity = payload["sub"]
        if not isinstance(identity, str) or not identity:
            raise AuthHTTPException(
                detail="无效或已过期的令牌", headers={"WWW-Authenticate": "Bearer"}
            )
        if identity == SERVICE_IDENTITY:
            return {
                "identity": identity,
                "display_name": "Subagent Service",
                "permissions": ["read"],
                "is_authenticated": True,
                "user_spaces": [],
            }
        user_table = await user_repo.get(identity=identity)
        user_spaces =await user_space_repo.list_by_user(identity=identity)
        if user_table is None:
            raise AuthHTTPException(
                detail="用户不存在", headers={"WWW-Authenticate": "Bearer"}
            )
        return {
            "identity": identity,
            "display_name": user_table.display_name,
            "permissions": ["read", "write"] if user_table.is_super else ["read"],
            "is_authenticated": True,
            "user_spaces":user_spaces
        }


# ---------- 配额：一个用户能拥有多少个会话（ADR-0012） ----------
#
# 这是替代 MAX_ACTIVE_TASKS 的那道闸：不再限制一个主管派多少子 agent，而是限制
# 用户拥有多少 thread（子线程也计数，它们的 metadata 带 owner_identity）。
# 超管不受限：管理员要能排查别人的问题，且总数应当能一眼数清。
@auth.on.threads.create
async def _quota_threads_create(ctx, value):
    if "write" in (ctx.permissions or []):
        return None
    maker = get_session_maker()
    async with maker() as session:
        used = await ThreadQuotaRepository(session).count_owned(ctx.user.identity)
    if used >= MAX_THREADS_PER_USER:
        raise AuthHTTPException(
            status_code=403,
            detail=(
                f"会话数已达上限（{MAX_THREADS_PER_USER}），请先删除不用的会话再新建"
            ),
        )
    # 把属主写进 metadata：子线程的属主是服务账号，配额靠这个字段计数
    meta = value.get("metadata") or {}
    if not meta.get("owner_identity"):
        value["metadata"] = {**meta, "owner_identity": ctx.user.identity}
    return None



# 删会话时级联删它的子线程（ADR-0012）。
# 子线程的属主是服务账号，用户既看不见也删不掉；不跟着删就是永久孤儿，
# 一直占配额与 checkpoint 存储。这里删的是子线程，父线程交给原生接口继续删。
#
# 注意顺序：aegra 在**校验 ownership 之前**调用本 handler，所以这里要自己确认
# 这个 thread 属于调用者，否则任何人传别人的 thread_id 都能触发级联删除。
@auth.on.threads.delete
async def _cascade_child_threads(ctx, value):
    thread_id = (value or {}).get("thread_id")
    if not thread_id:
        return None  # prune 等批量路径不带单个 thread_id，跳过
    maker = get_session_maker()
    async with maker() as session:
        owner = await session.scalar(select(ThreadORM.user_id).where(ThreadORM.thread_id == thread_id))
        if owner is None or (owner != ctx.user.identity and "write" not in (ctx.permissions or [])):
            return None  # 交给原生接口去 404/拒绝，这里不动任何东西
        # 会话文件（ADR-0020）：表格派生按台账里的内容指纹整组删。派生可重建，所以
        # 先删它、后删线程：万一原生删除失败，代价只是下次查询重转一遍。原件前缀
        # 仍在 uploads/<owner>/<thread>/ 下，由 scripts/reap_upload_orphans.py 回收。
        shas = list(
            await session.scalars(
                select(SessionObject.sha256).where(
                    SessionObject.owner_identity == owner, SessionObject.thread_id == thread_id
                )
            )
        )
        repo = ThreadQuotaRepository(session)
        children = await repo.list_children(thread_id)
        if children:
            checkpointer = db_manager.get_checkpointer()
            for child in children:
                # 先删 checkpoint：这一步失败时 thread 行还在，客户端重试即可；
                # 反序则行已没、checkpoint 再也没人能找到（同 aegra delete_thread 的约定）
                await checkpointer.adelete_thread(child)
            await repo.delete_threads(children)
    if children:
        logger.info("thread_children_deleted", parent=thread_id, children=len(children))
    for sha256 in shas:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(_drop_derived, sha256)
    return None


def _drop_derived(sha256: str) -> None:
    """删一个内容指纹的派生缓存。惰导入：HTTP 启动路径不拖进图侧模块。"""
    from graphs.tools.xlsx import duck

    duck.drop_derived(sha256)
