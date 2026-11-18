"""账号流程：一次性 bootstrap 建超管、登录、超管建/删/重置账号。

不碰真库：`UserRepository` 换成内存实现，`get_session` 依赖换成空壳 session。
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aegra_api.core.auth_deps import require_auth
from aegra_api.models.auth import User
from custom.api import account
from custom.api.deps import get_session


class FakeRepo:
    def __init__(self) -> None:
        self.users: dict[str, SimpleNamespace] = {}

    async def count(self) -> int:
        return len(self.users)

    async def exists(self, identity: str) -> bool:
        return identity in self.users

    async def get(self, identity: str) -> SimpleNamespace | None:
        return self.users.get(identity)

    async def list_all(self) -> list[SimpleNamespace]:
        return list(self.users.values())

    async def create(
        self,
        identity: str,
        display_name: str,
        hashed_password: str,
        org_id: str | None = None,
        email: str | None = None,
        is_super: bool = False,
    ) -> SimpleNamespace:
        user = SimpleNamespace(
            identity=identity,
            display_name=display_name,
            hashed_password=hashed_password,
            is_super=is_super,
        )
        self.users[identity] = user
        return user

    async def update_password(self, identity: str, hashed_password: str) -> bool:
        if identity not in self.users:
            return False
        self.users[identity].hashed_password = hashed_password
        return True

    async def delete(self, identity: str) -> bool:
        return self.users.pop(identity, None) is not None


class FakeSession:
    async def execute(self, *args, **kwargs):
        return None

    async def rollback(self) -> None:
        return None


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    """(client, repo)：客户端按当前 override 的身份发请求。"""
    monkeypatch.setenv("JWT_SECRET", "test-secret-0123456789abcdefghijklmnop")
    monkeypatch.setenv("PASSWORD_PEPPER", "test-pepper")
    repo = FakeRepo()
    monkeypatch.setattr(account, "UserRepository", lambda session: repo)

    app = FastAPI()
    app.include_router(account.router)
    app.dependency_overrides[get_session] = lambda: FakeSession()

    def as_user(identity: str = "root", is_super: bool = True) -> None:
        app.dependency_overrides[require_auth] = lambda: User(
            identity=identity, permissions=["read", "write"] if is_super else ["read"]
        )

    as_user()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return client, repo, as_user


BOOT = {"identity": "root", "display_name": "管理员", "password": "root12345"}


async def test_first_run_flips_after_bootstrap(world) -> None:
    client, repo, _ = world
    assert (await client.get("/custom/first-run")).json() == {"first_run": True}

    res = await client.post("/custom/bootstrap", json=BOOT)

    assert res.status_code == 200
    assert res.json()["access_token"]
    assert repo.users["root"].is_super is True
    assert (await client.get("/custom/first-run")).json() == {"first_run": False}


async def test_bootstrap_is_one_shot(world) -> None:
    client, _, _ = world
    await client.post("/custom/bootstrap", json=BOOT)

    res = await client.post(
        "/custom/bootstrap",
        json={"identity": "intruder", "display_name": "x", "password": "intruder123"},
    )

    assert res.status_code == 403


async def test_bootstrap_rejects_reserved_identity(world) -> None:
    client, repo, _ = world

    res = await client.post(
        "/custom/bootstrap",
        json={"identity": "subagent_service", "display_name": "x", "password": "whatever123"},
    )

    assert res.status_code == 422
    assert repo.users == {}


async def test_login_after_bootstrap(world) -> None:
    client, _, _ = world
    await client.post("/custom/bootstrap", json=BOOT)

    assert (await client.post("/custom/login", json={"identity": "root", "password": "root12345"})).status_code == 200
    # >72 字节的密码在 bcrypt 5 会抛 ValueError，必须还是 401 而不是 500
    too_long = "a" * 100
    assert (await client.post("/custom/login", json={"identity": "root", "password": too_long})).status_code == 401


async def test_non_super_cannot_manage_users(world) -> None:
    client, _, as_user = world
    as_user("alice", is_super=False)

    res = await client.post(
        "/custom/users",
        json={"identity": "bob", "display_name": "bob", "password": "bob123456"},
    )

    assert res.status_code == 403


async def test_super_creates_lists_resets_and_deletes(world) -> None:
    client, repo, as_user = world
    as_user("root", is_super=True)

    created = await client.post(
        "/custom/users",
        json={"identity": "alice", "display_name": "爱丽丝", "password": "alice12345"},
    )
    assert created.status_code == 200
    assert created.json()["is_super"] is False
    assert [u["identity"] for u in (await client.get("/custom/users")).json()["users"]] == ["alice"]

    # 重复建同名 -> 409
    dup = await client.post(
        "/custom/users",
        json={"identity": "alice", "display_name": "爱丽丝", "password": "alice12345"},
    )
    assert dup.status_code == 409

    assert (await client.post("/custom/users/alice/password", json={"password": "newpass123"})).status_code == 200
    assert (await client.post("/custom/login", json={"identity": "alice", "password": "newpass123"})).status_code == 200
    assert (await client.post("/custom/users/ghost/password", json={"password": "newpass123"})).status_code == 404

    assert (await client.delete("/custom/users/alice")).status_code == 200
    assert "alice" not in repo.users

    # 删自己 -> 400
    assert (await client.delete("/custom/users/root")).status_code == 400
