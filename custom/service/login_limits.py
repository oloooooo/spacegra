"""登录限流：IP 维度的滑动窗口（ADR-0019）。

账号级锁定在库里（`user_tables.login_failed_count`，5 次锁 300 秒），只能防"盯一个账号
猛试"；这里补 IP 维度，防"换着账号穷举"和"一个 IP 扫一片"。计数放 Redis：

- 跨副本、跨重启生效（进程内计数在多副本下等于阈值 ×N）；
- 成功登录只清「IP + 账号」那一格，同 IP 下别人账号的失败记录留着；
- Redis 连不上时**不拦**（fail-open）并打日志：限流是附加防线，不能让它成为登录的单点。

信任前提：客户端 IP 取 `X-Forwarded-For` 首段（与访问日志一致）。生产部署必须由反向
代理覆盖/剥离客户端自带的 XFF，否则伪造 XFF 即可绕过本限流。
"""

from __future__ import annotations

import hashlib
import os
import time
from uuid import uuid4

import redis.asyncio as aioredis
import structlog

logger = structlog.getLogger(__name__)

# 阈值比账号锁定（5 次）宽：正常用户先撞到"账号被锁定"的明确提示，而不是 IP 限流
LOGIN_FAILURE_WINDOW_SECONDS = 600
LOGIN_FAILURE_IP_ACCOUNT_MAX = 10
LOGIN_FAILURE_IP_MAX = 30

_KEY_PREFIX = "aegra:login-failure"
_client: aioredis.Redis | None = None


def _redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    return _client


def _ip_key(ip: str) -> str:
    return f"{_KEY_PREFIX}:ip:{ip}"


def _ip_account_key(ip: str, identifier: str) -> str:
    digest = hashlib.sha1(f"{ip}|{identifier}".encode()).hexdigest()
    return f"{_KEY_PREFIX}:ipacct:{digest}"


def extract_client_ip(request) -> str:
    """客户端 IP：XFF 首段优先（见模块头的信任前提），其次 TCP 对端。"""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def check_login_rate_limit(ip: str, identifier: str) -> tuple[bool, int]:
    """返回 (是否放行, 需要等待的秒数)。被拦时秒数已按窗口上限截断。"""
    try:
        redis = _redis()
        now = time.time()
        window = LOGIN_FAILURE_WINDOW_SECONDS
        for key, max_failures in (
            (_ip_account_key(ip, identifier), LOGIN_FAILURE_IP_ACCOUNT_MAX),
            (_ip_key(ip), LOGIN_FAILURE_IP_MAX),
        ):
            await redis.zremrangebyscore(key, 0, now - window)
            if await redis.zcard(key) < max_failures:
                continue
            oldest = await redis.zrange(key, 0, 0, withscores=True)
            if not oldest:
                continue
            retry_after = int(oldest[0][1] + window - now) + 1
            return False, max(1, min(window, retry_after))
        return True, 0
    except Exception:  # noqa: BLE001 —— Redis 故障不能让登录不可用
        logger.warning("login rate limit skipped: redis unavailable", ip=ip, exc_info=True)
        return True, 0


async def record_login_failure(ip: str, identifier: str) -> None:
    """在两个窗口各记一笔失败（用时间戳做 score，天然去重排序）。"""
    try:
        redis = _redis()
        now = time.time()
        window = LOGIN_FAILURE_WINDOW_SECONDS
        member = f"{now}-{uuid4().hex[:8]}"
        for key in (_ip_account_key(ip, identifier), _ip_key(ip)):
            await redis.zremrangebyscore(key, 0, now - window)
            await redis.zadd(key, {member: now})
            await redis.expire(key, window)
    except Exception:  # noqa: BLE001
        logger.warning("login failure not recorded: redis unavailable", ip=ip, exc_info=True)


async def clear_login_failures(ip: str, identifier: str) -> None:
    """登录成功：只清这一对 IP+账号；IP 全局计数保留（别的账号还在被试）。"""
    try:
        await _redis().delete(_ip_account_key(ip, identifier))
    except Exception:  # noqa: BLE001
        logger.warning("login failures not cleared: redis unavailable", ip=ip, exc_info=True)
