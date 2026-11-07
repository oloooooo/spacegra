"""清掉没有台账行的上传文件（ADR-0018）。

删会话时 `session_object` 行靠外键 CASCADE 跟着删（迁移 `b8c9d0e1f2a3`），本地盘上的
字节不会 —— 这个脚本扫 `data/uploads/<owner>/<thread>/`，把"台账里已经没有这个会话"
且文件**够老**（默认 24h，避开上传写字节与写行之间的窗口）的目录删掉。

    PYTHONPATH=. uv run python scripts/reap_upload_orphans.py              # 只看（默认）
    PYTHONPATH=. uv run python scripts/reap_upload_orphans.py --apply      # 真删
    PYTHONPATH=. uv run python scripts/reap_upload_orphans.py --apply --older-than-hours 6

没有台账行的目录**不一定**是孤儿：可能上传崩在"字节已写、行未落"之间。
所以默认 24h，宁可留一会儿也不删刚写进来的东西。
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from custom.db.models import SessionObject
from shared.session import uploads


@dataclass
class Report:
    """一次扫描的结果，便于单测与 dry-run 打印。"""

    scanned_prefixes: int = 0
    live_prefixes: int = 0
    orphan_prefixes: list[str] = field(default_factory=list)
    orphan_objects: int = 0
    orphan_bytes: int = 0


def _iter_prefixes() -> dict[str, list[dict]]:
    """按 `owner/thread/` 聚合的上传文件（用 mtime 当"新鲜度"）。"""
    out: dict[str, list[dict]] = defaultdict(list)
    for owner, thread, _name, path in uploads.iter_local_keys():
        try:
            st = path.stat()
        except OSError:
            continue
        out[f"{owner}/{thread}/"].append(
            {
                "Key": f"{owner}/{thread}/{path.name}",
                "Size": st.st_size,
                "LastModified": datetime.fromtimestamp(st.st_mtime, UTC),
            }
        )
    return out


def _live_prefixes(rows: Iterable[Any]) -> set[str]:
    """台账里的活跃前缀（`<owner>/<thread>/`）。"""
    return {f"{row[0]}/{row[1]}/" for row in rows}


def _orphans(live: set[str], cutoff: datetime) -> tuple[list[str], int, int]:
    """该删的前缀 → `(前缀列表, 文件数, 字节数)`。"""
    prefixes: list[str] = []
    objects = 0
    size = 0
    for prefix, files in _iter_prefixes().items():
        if prefix in live:
            continue
        if any(f["LastModified"] >= cutoff for f in files):
            continue  # 可能有上传正在写，这一轮不动它
        prefixes.append(prefix)
        objects += len(files)
        size += sum(int(f["Size"] or 0) for f in files)
    return prefixes, objects, size


async def plan(older_than: timedelta) -> Report:
    """算出该删哪些目录（不删任何东西）。"""
    from aegra_api.core.database import db_manager
    from aegra_api.core.orm import get_session_maker
    from aegra_api.settings import settings

    # 只起 SQLAlchemy engine：脚本不需要 checkpointer/store（psycopg 池会和服务的连接抢额度）
    if db_manager.engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        db_manager.engine = create_async_engine(settings.db.database_url, pool_size=2, pool_pre_ping=True)

    prefixes = _iter_prefixes()
    report = Report(scanned_prefixes=len(prefixes))
    cutoff = datetime.now(UTC) - older_than

    maker = get_session_maker()
    async with maker() as session:
        rows = (await session.execute(select(SessionObject.owner_identity, SessionObject.thread_id))).all()
    live = _live_prefixes(rows)
    report.live_prefixes = len(live & set(prefixes))

    orphans, objects, size = _orphans(live, cutoff)
    report.orphan_prefixes = orphans
    report.orphan_objects = objects
    report.orphan_bytes = size
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="清掉没有台账行的上传文件")
    ap.add_argument("--apply", action="store_true", help="真的删（默认只 dry-run）")
    ap.add_argument("--older-than-hours", type=float, default=24.0)
    args = ap.parse_args()

    report = asyncio.run(plan(timedelta(hours=args.older_than_hours)))
    print(
        f"扫描目录 {report.scanned_prefixes}，活跃 {report.live_prefixes}，"
        f"孤儿 {len(report.orphan_prefixes)} 个（{report.orphan_objects} 个文件，"
        f"{report.orphan_bytes / 1024 / 1024:.1f}MB）"
    )
    for prefix in report.orphan_prefixes[:20]:
        print("  孤儿:", prefix)
    if not args.apply:
        print("dry-run：加 --apply 才会真删")
        return
    for prefix in report.orphan_prefixes:
        owner, thread = prefix.rstrip("/").split("/")[:2]
        try:
            uploads.delete_thread(owner, thread)
        except ValueError as e:  # 遗留的非法目录名：跳过，其他目录照删
            print(f"  跳过 {prefix}（{e}）")
            continue
        print(f"  已删 {prefix}")


if __name__ == "__main__":
    main()
