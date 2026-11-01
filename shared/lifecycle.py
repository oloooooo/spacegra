"""应用退出时要跑的异步清理钩子。

为什么放在 `shared/`：`custom/` 不许 import `graphs/`（`tests/unit/test_layout.py` 守着，
理由是 HTTP 启动路径不该拖进整张图）。但 `custom/app.py` 的 lifespan 确实需要在退出时
关掉图侧的资源池（Mirage 的 Workspace 池，ADR-0018）——那就让图侧在导入时把清理函数
登记到这里，HTTP 侧只调 `close_all()`，两边都不越线。

用法::

    # graphs/access/mirage_backend.py
    shared.lifecycle.register(close_all_workspaces)

    # custom/app.py 的 lifespan
    await shared.lifecycle.close_all()
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

logger = structlog.getLogger(__name__)

_closers: list[Callable[[], Awaitable[None]]] = []


def register(closer: Callable[[], Awaitable[None]]) -> None:
    """登记一个退出清理函数（同一函数重复登记只算一次）。"""
    if closer not in _closers:
        _closers.append(closer)


async def close_all() -> None:
    """按登记的逆序跑一遍；任何一个失败只记日志，不影响其他清理。"""
    while _closers:
        closer = _closers.pop()
        try:
            await closer()
        except Exception:  # noqa: BLE001 —— 清理失败不该让 shutdown 挂掉
            logger.exception("lifecycle_closer_failed", closer=getattr(closer, "__name__", repr(closer)))
