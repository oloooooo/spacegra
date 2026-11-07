"""`POST /custom/uploads` 的前置校验：属主 + "会话正在跑就别传"。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from custom.api.uploads import _authorize_thread

USER = SimpleNamespace(identity="alice")


class _Session:
    """按顺序吐出 `scalar()` 的返回值：第一条查属主，第二条查在跑的 run。"""

    def __init__(self, *values: object) -> None:
        self.values = list(values)
        self.queries = 0

    async def scalar(self, _stmt: object) -> object:
        self.queries += 1
        return self.values.pop(0)


async def test_unknown_thread_is_404() -> None:
    with pytest.raises(HTTPException) as caught:
        await _authorize_thread(_Session(None), "t1", USER)  # type: ignore[arg-type]

    assert caught.value.status_code == 404


async def test_someone_elses_thread_is_403() -> None:
    with pytest.raises(HTTPException) as caught:
        await _authorize_thread(_Session("bob"), "t1", USER)  # type: ignore[arg-type]

    assert caught.value.status_code == 403


async def test_running_thread_rejects_upload() -> None:
    with pytest.raises(HTTPException) as caught:
        await _authorize_thread(_Session("alice", "run-1"), "t1", USER)  # type: ignore[arg-type]

    assert caught.value.status_code == 409


async def test_idle_thread_passes() -> None:
    session = _Session("alice", None)

    await _authorize_thread(session, "t1", USER)  # type: ignore[arg-type]

    assert session.queries == 2
