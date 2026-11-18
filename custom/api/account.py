"""首次初始化 / 登录 / whoami / 账号管理：bcrypt+pepper 存密码，登录签发 JWT access token。

没有自助注册：只有用户表为空时（第一次访问）能用 `POST /custom/bootstrap` 建出第一个
超级用户，之后账号一律由超级用户创建。抄的是 yuxi 的 initialize + 用户管理，
不做 OAuth / OIDC。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aegra_api.core.auth_deps import require_auth
from aegra_api.models.auth import User
from aegra_api.models.errors import get_error_type
from custom.api.deps import get_session
from custom.api.schemas import (
    BootstrapRequest,
    CreateUserRequest,
    LoginRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserOut,
)
from custom.db.repositories import UserRepository
from custom.service.account import (
    _check_display_name,
    _check_identity,
    _check_password,
    _hash_password,
    _verify_password,
)
from custom.service.login_limits import (
    check_login_rate_limit,
    clear_login_failures,
    extract_client_ip,
    record_login_failure,
)
from shared.token import TOKEN_TTL, create_access_token

logger = structlog.getLogger(__name__)

router = APIRouter(prefix="/custom", tags=["/custom"])

# bootstrap 的 pg advisory 锁键（任意常数）：不加锁时并发首访会各数到 0 各建一个超管
_BOOTSTRAP_LOCK_KEY = 0x41454752


async def require_super(user: User = Depends(require_auth)) -> User:
    if "write" not in user.permissions:
        raise HTTPException(status_code=403, detail="只有超级用户可以管理账号")
    return user


def _token(identity: str) -> TokenResponse:
    return TokenResponse(access_token=create_access_token(identity), expires_in=TOKEN_TTL)


def _out(user) -> UserOut:
    return UserOut(identity=user.identity, display_name=user.display_name, is_super=user.is_super)


@router.get("/first-run")
async def first_run(session=Depends(get_session)) -> dict[str, bool]:
    """前端用来决定显示“初始化”还是“登录”，所以不鉴权。"""
    return {"first_run": await UserRepository(session).count() == 0}


@router.post("/bootstrap", response_model=TokenResponse)
async def bootstrap(body: BootstrapRequest, session=Depends(get_session)) -> TokenResponse:
    """第一次访问：创建唯一的超级用户并直接登录；之后永久 403。"""
    _check_identity(body.identity)
    _check_display_name(body.display_name)
    _check_password(body.password)
    repo = UserRepository(session)
    # “数用户 -> 插入”必须原子，否则并发首访会建出两个超管（yuxi 同款 advisory 事务锁）
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BOOTSTRAP_LOCK_KEY})
    if await repo.count() > 0:
        raise HTTPException(status_code=403, detail="系统已初始化，请直接登录")
    user = await repo.create(
        identity=body.identity,
        display_name=body.display_name.strip(),
        hashed_password=_hash_password(body.password),
        is_super=True,
    )
    logger.info("bootstrap created super user", identity=user.identity)
    return _token(user.identity)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, session=Depends(get_session)) -> TokenResponse | JSONResponse:
    """密码登录。两层限流：IP 滑动窗口（429）+ 账号级锁定（403），见 ADR-0019。"""
    ip = extract_client_ip(request)
    allowed, retry_after = await check_login_rate_limit(ip, body.identity)
    if not allowed:
        return _rate_limited(429, "登录尝试过于频繁，请稍后再试", retry_after)

    repo = UserRepository(session)
    user = await repo.get(body.identity)
    # 账号不存在与密码错误返回同一提示，避免账号枚举（未知账号也计一笔失败）
    if user is None:
        await record_login_failure(ip, body.identity)
        raise HTTPException(status_code=401, detail="账号或密码错误")

    # 锁定中不验密码；到期后先清零，避免解锁后的第一次失误立刻又锁上
    if user.is_login_locked():
        return _locked(user)
    if user.login_locked_until is not None:
        user.reset_failed_login()
        await repo.save(user)

    if not _verify_password(body.password, user.hashed_password):
        await record_login_failure(ip, body.identity)
        user.increment_failed_login()
        await repo.save(user)
        if user.is_login_locked():
            return _locked(user)
        raise HTTPException(status_code=401, detail="账号或密码错误")

    if user.login_failed_count or user.login_locked_until is not None:
        user.reset_failed_login()
        await repo.save(user)
    await clear_login_failures(ip, body.identity)
    return _token(user.identity)


def _locked(user) -> JSONResponse:
    """锁定期的 403。"""
    return _rate_limited(
        403, f"由于多次登录失败，账户已被锁定 {user.lock_remaining_seconds()} 秒", user.lock_remaining_seconds()
    )


def _rate_limited(status: int, message: str, retry_after: int) -> JSONResponse:
    """限流 / 锁定的错误响应。

    自己造 JSONResponse 而不 raise HTTPException：aegra 的全局 HTTPException handler
    重建响应时会漏掉 `exc.headers`，Retry-After 到不了浏览器（前端就没法倒计时）。
    错误体保持 aegra 的 {error, message, details} 形状。
    """
    return JSONResponse(
        status_code=status,
        content={
            "error": "too_many_requests" if status == 429 else get_error_type(status),
            "message": message,
            "details": None,
        },
        headers={"Retry-After": str(retry_after), "X-Lock-Remaining": str(retry_after)},
    )


@router.get("/whoami")
async def whoami(user: User = Depends(require_auth)):
    return {
        "identity": user.identity,
        "display_name": user.display_name,
        "permissions": user.permissions,
    }


@router.get("/users")
async def list_users(_: User = Depends(require_super), session=Depends(get_session)) -> dict[str, list[UserOut]]:
    return {"users": [_out(u) for u in await UserRepository(session).list_all()]}


@router.post("/users", response_model=UserOut)
async def create_user(
    body: CreateUserRequest,
    _: User = Depends(require_super),
    session=Depends(get_session),
) -> UserOut:
    """超级用户给个人开号（分配空间还要走 /custom/spaces/{name}/users）。"""
    _check_identity(body.identity)
    _check_display_name(body.display_name)
    _check_password(body.password)
    repo = UserRepository(session)
    if await repo.exists(body.identity):
        raise HTTPException(status_code=409, detail="账号名已存在")
    try:
        user = await repo.create(
            identity=body.identity,
            display_name=body.display_name.strip(),
            hashed_password=_hash_password(body.password),
            is_super=body.is_super,
        )
    except IntegrityError:
        # exists() 与 create() 之间是 TOCTOU：并发建同名会撞主键，归一成 409 而不是 500
        await session.rollback()
        raise HTTPException(status_code=409, detail="账号名已存在") from None
    return _out(user)


@router.post("/users/{identity}/password")
async def reset_password(
    identity: str,
    body: ResetPasswordRequest,
    _: User = Depends(require_super),
    session=Depends(get_session),
) -> dict[str, str]:
    _check_password(body.password)
    if not await UserRepository(session).update_password(identity, _hash_password(body.password)):
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"identity": identity}


@router.delete("/users/{identity}")
async def delete_user(
    identity: str,
    user: User = Depends(require_super),
    session=Depends(get_session),
) -> dict[str, str]:
    if identity == user.identity:
        raise HTTPException(status_code=400, detail="不能删除自己")
    repo = UserRepository(session)
    target = await repo.get(identity)
    if target is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    await repo.delete(identity)
    return {"identity": identity}
