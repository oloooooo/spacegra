"""IP 滑动窗口限流：假 Redis 驱动真实 zset 逻辑（不连库、不连 Redis）。"""

import time
from types import SimpleNamespace

import pytest

from custom.service import login_limits as limits


class FakeRedis:
    """只实现 login_limits 用到的那几个命令，行为对齐 redis-py asyncio。"""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}

    async def zremrangebyscore(self, key: str, low: float, high: float) -> None:
        self.zsets[key] = {m: s for m, s in self.zsets.get(key, {}).items() if not low <= s <= high}

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    async def zrange(self, key: str, start: int, stop: int, withscores: bool = False):
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])[start : stop + 1]
        return items if withscores else [m for m, _ in items]

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.zsets.setdefault(key, {}).update(mapping)

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def delete(self, key: str) -> None:
        self.zsets.pop(key, None)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    redis = FakeRedis()
    monkeypatch.setattr(limits, "_client", redis)
    return redis


async def test_ip_account_window_blocks_at_ten_failures(fake: FakeRedis) -> None:
    for _ in range(limits.LOGIN_FAILURE_IP_ACCOUNT_MAX - 1):
        assert await limits.check_login_rate_limit("1.2.3.4", "alice") == (True, 0)
        await limits.record_login_failure("1.2.3.4", "alice")
    assert await limits.check_login_rate_limit("1.2.3.4", "alice") == (True, 0)

    await limits.record_login_failure("1.2.3.4", "alice")

    allowed, retry_after = await limits.check_login_rate_limit("1.2.3.4", "alice")
    assert allowed is False
    assert 1 <= retry_after <= limits.LOGIN_FAILURE_WINDOW_SECONDS
    # 同一 IP 换个账号还能进：组合窗口不牵连别人
    assert await limits.check_login_rate_limit("1.2.3.4", "bob") == (True, 0)


async def test_ip_wide_window_blocks_after_thirty_failures(fake: FakeRedis) -> None:
    for i in range(limits.LOGIN_FAILURE_IP_MAX):
        await limits.record_login_failure("1.2.3.4", f"user{i}")

    allowed, retry_after = await limits.check_login_rate_limit("1.2.3.4", "newcomer")

    assert allowed is False
    assert retry_after >= 1
    # 别的 IP 不受影响
    assert await limits.check_login_rate_limit("5.6.7.8", "newcomer") == (True, 0)


async def test_success_clears_only_that_account(fake: FakeRedis) -> None:
    for _ in range(limits.LOGIN_FAILURE_IP_ACCOUNT_MAX):
        await limits.record_login_failure("1.2.3.4", "alice")
    await limits.record_login_failure("1.2.3.4", "bob")

    await limits.clear_login_failures("1.2.3.4", "alice")

    assert await limits.check_login_rate_limit("1.2.3.4", "alice") == (True, 0)
    # bob 自己的记录还在，IP 全局窗口也只统计不清（要防的就是“换账号接着试”）
    assert await limits.check_login_rate_limit("1.2.3.4", "bob") == (True, 0)
    assert await fake.zcard(limits._ip_key("1.2.3.4")) == limits.LOGIN_FAILURE_IP_ACCOUNT_MAX + 1


async def test_old_failures_fall_out_of_window(fake: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    for _ in range(limits.LOGIN_FAILURE_IP_ACCOUNT_MAX):
        await limits.record_login_failure("1.2.3.4", "alice")
    # 把时间拨到一个窗口之后：老记录应被清掉，窗口重新放行
    later = time.time() + limits.LOGIN_FAILURE_WINDOW_SECONDS + 1
    monkeypatch.setattr(limits.time, "time", lambda: later)

    assert await limits.check_login_rate_limit("1.2.3.4", "alice") == (True, 0)


async def test_redis_down_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        async def zremrangebyscore(self, *args, **kwargs):
            raise ConnectionError("redis down")

        async def zadd(self, *args, **kwargs):
            raise ConnectionError("redis down")

        async def delete(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(limits, "_client", Broken())

    assert await limits.check_login_rate_limit("1.2.3.4", "alice") == (True, 0)
    await limits.record_login_failure("1.2.3.4", "alice")  # 不抛
    await limits.clear_login_failures("1.2.3.4", "alice")


async def test_client_ip_prefers_forwarded_for_header() -> None:
    request = SimpleNamespace(
        headers={"x-forwarded-for": " 9.9.9.9 , 10.0.0.1"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    assert limits.extract_client_ip(request) == "9.9.9.9"
    assert limits.extract_client_ip(SimpleNamespace(headers={}, client=SimpleNamespace(host="1.1.1.1"))) == "1.1.1.1"
