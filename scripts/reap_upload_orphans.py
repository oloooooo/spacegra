"""清掉没有台账行的上传文件（ADR-0018）。

删会话时 `session_object` 行靠外键 CASCADE 跟着删（迁移 `b8c9d0e1f2a3`），字节不会 ——
这个脚本负责扫介质的 `<owner>/<thread>/` 前缀（对象存储的 `uploads/` 或本地的
`data/uploads/`），把"台账里已经没有这个会话"且文件**够老**（默认 24h，避开上传写
字节与写行之间的窗口）的东西删掉。两种介质都扫，介质由 `uploads.store` 自动决定。

    PYTHONPATH=. uv run python scripts/reap_upload_orphans.py --dry-run      # 只看（默认）
    PYTHONPATH=. uv run python scripts/reap_upload_orphans.py --apply        # 真删
    PYTHONPATH=. uv run python scripts/reap_upload_orphans.py --apply --older-than-hours 6

没有台账行的前缀**不一定**是孤儿：可能上传崩在"字节已写、行未落"之间。
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
from shared.session import blobs, uploads


@dataclass
class Report:
    """一次扫描的结果，便于单测与 dry-run 打印。"""

    scanned_prefixes: int = 0
    live_prefixes: int = 0
    orphan_prefixes: list[str] = field(default_factory=list)
    orphan_objects: int = 0
    orphan_bytes: int = 0
    local_prefixes: list[str] = field(default_factory=list)
    local_objects: int = 0
    local_bytes: int = 0


def _iter_prefixes(max_keys: int = 100_000) -> dict[str, list[dict]]:
    """列出 `uploads/` 下按 `owner/thread/` 聚合的对象（key -> 对象元数据）。"""
    s3 = blobs.client()
    bucket = blobs.bucket()
    out: dict[str, list[dict]] = defaultdict(list)
    token: str | None = None
    seen = 0
    while True:
        kw = {"Bucket": bucket, "Prefix": "uploads/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            key = obj["Key"]
            parts = key.split("/")
            if len(parts) < 4:  # uploads/<owner>/<thread>/<name>
                continue
            out[f"{parts[0]}/{parts[1]}/{parts[2]}/"].append(obj)
        seen += len(page.get("Contents", []))
        if seen >= max_keys or not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")


def _iter_local_prefixes() -> dict[str, list[dict]]:
    """本地介质里按 `owner/thread/` 聚合的上传文件（用 mtime 当"新鲜度"）。"""
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


def _live_prefixes(rows: Iterable[Any]) -> tuple[set[str], set[str]]:
    """台账里的活跃前缀 → ``(本地形状 <owner>/<thread>/，s3 形状 uploads/<owner>/<thread>/)``。"""
    pairs = [(str(row[0]), str(row[1])) for row in rows]
    return (
        {f"{owner}/{thread}/" for owner, thread in pairs},
        {blobs.upload_prefix(owner, thread) for owner, thread in pairs},
    )


def _local_orphans(live: set[str], cutoff: datetime) -> tuple[list[str], int, int]:
    """本地介质里该删的前缀 → `(前缀列表, 文件数, 字节数)`。"""
    prefixes: list[str] = []
    objects = 0
    size = 0
    for prefix, files in _iter_local_prefixes().items():
        if prefix in live:
            continue
        if any(f["LastModified"] >= cutoff for f in files):
            continue
        prefixes.append(prefix)
        objects += len(files)
        size += sum(int(f["Size"] or 0) for f in files)
    return prefixes, objects, size


async def plan(older_than: timedelta, *, max_keys: int = 100_000) -> Report:
    """算出该删哪些前缀（不删任何东西）。"""
    from aegra_api.core.database import db_manager
    from aegra_api.core.orm import get_session_maker
    from aegra_api.settings import settings

    # 只起 SQLAlchemy engine：脚本不需要 checkpointer/store（psycopg 池会和服务的连接抢额度）
    if db_manager.engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        db_manager.engine = create_async_engine(settings.db.database_url, pool_size=2, pool_pre_ping=True)

    prefixes: dict[str, list[dict]] = {}
    if uploads.store_mode() == "s3":
        prefixes = _iter_prefixes(max_keys)
    report = Report(scanned_prefixes=len(prefixes))
    cutoff = datetime.now(UTC) - older_than

    maker = get_session_maker()
    async with maker() as session:
        rows = (await session.execute(select(SessionObject.owner_identity, SessionObject.thread_id))).all()
    # 本地前缀是 ``<owner>/<thread>/``，s3 前缀是 ``uploads/<owner>/<thread>/``——
    # 两套形状共用一份 live 集合时 s3 分支永远不命中，所有 prefix 都会被当孤儿删掉。
    live_local, live_s3 = _live_prefixes(rows)

    for prefix, objects in prefixes.items():
        if prefix in live_s3:
            report.live_prefixes += 1
            continue
        fresh = [o for o in objects if o.get("LastModified") and o["LastModified"] >= cutoff]
        if fresh:
            continue  # 可能有上传正在写，这一轮不动它
        report.orphan_prefixes.append(prefix)
        report.orphan_objects += len(objects)
        report.orphan_bytes += sum(int(o.get("Size") or 0) for o in objects)

    local_prefixes, local_objects, local_bytes = _local_orphans(live_local, cutoff)
    report.local_prefixes = local_prefixes
    report.local_objects = local_objects
    report.local_bytes = local_bytes
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="清掉没有台账行的上传对象")
    ap.add_argument("--apply", action="store_true", help="真的删（默认只 dry-run）")
    ap.add_argument("--older-than-hours", type=float, default=24.0)
    ap.add_argument(
        "--xlsx-cache-days",
        type=float,
        default=0.0,
        help="同时清掉对象存储里超过 N 天的 xlsx 派生缓存（默认 0 = 不清）",
    )
    args = ap.parse_args()

    report = asyncio.run(plan(timedelta(hours=args.older_than_hours)))
    print(
        f"扫描前缀 {report.scanned_prefixes}，活跃 {report.live_prefixes}，"
        f"孤儿 {len(report.orphan_prefixes)} 个（{report.orphan_objects} 个对象，"
        f"{report.orphan_bytes / 1024 / 1024:.1f}MB）"
    )
    print(
        f"本地介质孤儿 {len(report.local_prefixes)} 个（{report.local_objects} 个文件，{report.local_bytes / 1024 / 1024:.1f}MB）"
    )
    for prefix in report.orphan_prefixes[:20]:
        print("  孤儿:", prefix)
    if args.xlsx_cache_days > 0:
        from graphs.tools.xlsx import duck

        objects, size = duck.prune_remote(args.xlsx_cache_days, dry_run=not args.apply)
        print(f"xlsx 派生缓存：{objects} 个对象（{size / 1024 / 1024:.1f}MB）")
    if not args.apply:
        print("dry-run：加 --apply 才会真删")
        return
    for prefix in report.orphan_prefixes:
        deleted = blobs.delete_prefix(prefix)
        print(f"  已删 {prefix} → {deleted} 个对象")
    for prefix in report.local_prefixes:
        owner, thread = prefix.rstrip("/").split("/")[:2]
        try:
            uploads.delete_thread(owner, thread)
        except ValueError as e:  # 遗留的非法目录名：跳过，其他目录照删
            print(f"  跳过 {prefix}（{e}）")
            continue
        print(f"  已删本地 {prefix}")


if __name__ == "__main__":
    main()
