"""上传目录隔离键 `upload_scope`：客户端的 configurable 不能决定 owner/thread。

回归依据（issue #2）：主管 run 的 configurable 可能是客户端写的（assistant.config、cron、
`if not context: context = configurable.copy()`），而 `uploads/<owner>/<thread>/` 直接决定
能读到哪些上传文件。只有服务账号的子 agent run 才认拦截层转发的 `upload_owner`/`upload_thread`。
"""

from __future__ import annotations

import pytest

from graphs.access.spaces import upload_scope
from shared.token import SERVICE_IDENTITY


@pytest.mark.parametrize("owner", ["victim", "../victim", ""])
def test_supervisor_owner_is_own_identity(owner: str) -> None:
    cfg = {
        "configurable": {
            "langgraph_auth_user": {"identity": "u1"},
            "upload_owner": owner,
            "upload_thread": "受害者的线程",
        }
    }

    assert upload_scope(cfg) == ("u1", "no-thread")


def test_supervisor_keeps_own_thread() -> None:
    cfg = {"configurable": {"langgraph_auth_user": {"identity": "u1"}, "thread_id": "t1", "upload_owner": "victim"}}

    assert upload_scope(cfg) == ("u1", "t1")


def test_subagent_run_honours_forwarded_scope() -> None:
    """拦截层 `_config_for` 只写这三个键（不拷贝客户端 config），转发可信。"""
    cfg = {
        "configurable": {
            "langgraph_auth_user": {"identity": SERVICE_IDENTITY},
            "upload_owner": "u1",
            "upload_thread": "t1",
        }
    }

    assert upload_scope(cfg) == ("u1", "t1")


def test_no_auth_user_falls_back_to_local() -> None:
    assert upload_scope({}) == ("local", "no-thread")
