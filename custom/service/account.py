"""账号密码与 token 校验（HTTP 侧）：bcrypt+pepper 哈希、token -> identity、空间计数。

签发在 `shared/token.py`（图侧也要用）；这里只放“失败要映射成 HTTP 异常”的部分。
"""

import os
import re

import bcrypt
import jwt
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from custom.db.models import UserSpace
from shared.token import SERVICE_IDENTITY

# 不能被占用的账号名：服务身份在鉴权中间件里免查库放行；`local` 是 run config 缺身份时的兜底
RESERVED_IDENTITIES = frozenset({SERVICE_IDENTITY, "local"})

_IDENTITY_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
_DISPLAY_NAME_MAX = 10  # user_tables.display_name 是 String(10)
_PASSWORD_MIN = 8
_PASSWORD_MAX_BYTES = 72  # bcrypt 只吃前 72 字节，5.x 超长直接抛 ValueError


def _check_identity(identity: str) -> None:
    """账号名规则：字母/数字/下划线 3-20 位，且不能是保留身份。"""
    if not _IDENTITY_RE.match(identity) or identity in RESERVED_IDENTITIES:
        raise HTTPException(
            status_code=422,
            detail="账号名只能用字母/数字/下划线，3-20 位",
        )


def _check_display_name(display_name: str) -> None:
    if not display_name.strip() or len(display_name) > _DISPLAY_NAME_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"显示名不能为空且不超过 {_DISPLAY_NAME_MAX} 个字符",
        )


def _check_password(password: str) -> None:
    if len(password) < _PASSWORD_MIN or len(password.encode()) > _PASSWORD_MAX_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"密码至少 {_PASSWORD_MIN} 位，且不超过 {_PASSWORD_MAX_BYTES} 字节（UTF-8）",
        )


def _hash_password(password: str) -> str:
    # pepper 来自 .env，与 bcrypt 盐（自动生成、嵌在哈希串中）叠加
    peppered = (password + os.environ["PASSWORD_PEPPER"]).encode()
    return bcrypt.hashpw(peppered, bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    peppered = (password + os.environ["PASSWORD_PEPPER"]).encode()
    try:
        return bcrypt.checkpw(peppered, hashed.encode())
    except ValueError:
        # bcrypt 5 对 >72 字节的密码抛 ValueError；登录按密码错误处理，不 500
        return False


def _decode_access_token(token: str) -> str:
    """
    接收access token
    返回user_id
    """
    payload = jwt.decode(
        token,
        os.environ["JWT_SECRET"],
        algorithms=["HS256"],  # 必须显式指定，避免 alg 混淆攻击
        options={"require": ["sub", "iat", "exp"]},
        leeway=0,  # 可按需给时钟偏移留几秒，例如 leeway=5
    )
    if payload is None or payload.get("type") or not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或已过期的令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    identity = payload.get("sub")
    if not isinstance(identity, str) or not identity:
        raise jwt.InvalidTokenError("Invalid 'sub' claim")
    return identity


async def count_user_spaces(session: AsyncSession, identity: str) -> int:
    """用户加入了多少个微服务空间。"""
    stmt = select(func.count()).where(UserSpace.identity == identity)
    return (await session.execute(stmt)).scalar_one()


async def count_space_users(session: AsyncSession, name: str) -> int:
    """一个微服务空间有多少用户。"""
    stmt = select(func.count()).where(UserSpace.space_name == name)
    return (await session.execute(stmt)).scalar_one()
