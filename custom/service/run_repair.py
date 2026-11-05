"""启动时修掉"进程死了但状态没写回去"的 run / thread（ADR-0018 §10 的上传前置）。

本地模式下 run 跑在进程里，进程一崩，`runs.status` 就永远停在 `running`、
`thread.status` 停在 `busy`。aegra 自带的租约回收只处理 **Redis 模式且带租约**的
行（`src/aegra_api/services/lease_reaper.py:100-108`），`lease_expires_at IS NULL`
的本地行没人管 —— 于是上传的"会话在跑就别传"会永久拒绝这个会话。

判据只有一条：**没有活着的租约**。有租约且没过期的一律不动（别的 worker 正在跑）。
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.getLogger(__name__)

_STALE_RUNS = text(
    """
    UPDATE runs
       SET status = 'error',
           error_message = '进程退出时 run 未收尾（启动自检回收）',
           updated_at = now()
     WHERE status = 'running'
       AND (lease_expires_at IS NULL OR lease_expires_at < now())
    """
)

_IDLE_THREADS = text(
    """
    UPDATE thread
       SET status = 'idle', updated_at = now()
     WHERE status = 'busy'
       AND NOT EXISTS (
           SELECT 1 FROM runs r
            WHERE r.thread_id = thread.thread_id
              AND r.status IN ('pending', 'running')
       )
    """
)


async def repair_stale_runs(session: AsyncSession) -> tuple[int, int]:
    """返回 `(改回 error 的 run 数, 改回 idle 的 thread 数)`。可重复执行。"""
    runs = (await session.execute(_STALE_RUNS)).rowcount or 0
    threads = (await session.execute(_IDLE_THREADS)).rowcount or 0
    await session.commit()
    if runs or threads:
        logger.warning("runs.repaired", runs=runs, threads=threads)
    return runs, threads
