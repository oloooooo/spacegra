"""上传 → 读 → 删会话 的端到端检查（ADR-0018 §9，本地介质版）。

要真 PG（默认 `localhost:5432`）、不需要对象存储，所以是**手动脚本**，不进 pytest。

    PYTHONPATH=. uv run python scripts/e2e_uploads_local.py

它检查六件事，任何一条不符就非零退出：

1. 三个上传落盘、`target_agent` 按文件名规则分流（空间表 / xlsx / md）；
2. 同名再传是覆盖（size 变），台账行只有一条；
3. `/uploads` 的 backend 看得见这些文件、内容与最后一次上传一致；
4. 越界路径（`..`）读不到；
5. 删会话 → 台账行随外键 CASCADE 消失；
6. 删会话后磁盘上还有字节（孤儿），`reap_upload_orphans.plan()` 能把它算出来。
"""

from __future__ import annotations

import asyncio
import io
import shutil
import sys
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aegra_api.settings import settings
from custom.service import uploads
from graphs.access.uploads_backend import filesystem_backend
from scripts.reap_upload_orphans import plan
from shared.session import uploads as upload_store
from shared.session.upload_rule import XLSX_AGENT

OWNER = "e2e_local"
THREAD = "e2e-upload-thread"
SPACES = ["cbam-reporting"]

failures: list[str] = []


def check(what: str, ok: bool, detail: str = "") -> None:
    print(f"{'OK ' if ok else 'BAD'} {what}{'' if ok else f' — {detail}'}")
    if not ok:
        failures.append(what)


def _config() -> dict:
    return {"configurable": {"langgraph_auth_user": {"identity": OWNER}, "thread_id": THREAD}}


async def _reset(maker) -> None:  # noqa: ANN001 —— async_sessionmaker
    async with maker() as s:
        await s.execute(text("DELETE FROM session_object WHERE owner_identity=:o"), {"o": OWNER})
        await s.execute(text("DELETE FROM thread WHERE thread_id=:t"), {"t": THREAD})
        await s.execute(text("DELETE FROM user_tables WHERE identity=:o"), {"o": OWNER})
        await s.execute(
            text(
                "INSERT INTO user_tables(identity, display_name, hashed_password, is_super) "
                "VALUES (:o, 'e2e', 'x', false)"
            ),
            {"o": OWNER},
        )
        await s.execute(
            text(
                "INSERT INTO thread(thread_id, status, user_id, created_at, updated_at) "
                "VALUES (:t, 'idle', :o, now(), now())"
            ),
            {"t": THREAD, "o": OWNER},
        )
        await s.commit()
    shutil.rmtree(upload_store.local_dir(OWNER, THREAD), ignore_errors=True)


async def main() -> int:
    engine = create_async_engine(settings.db.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    await _reset(maker)

    cases = [
        ("cbam-reporting_demo.xlsx", "cbam-reporting", b"sheet-1"),
        ("result1_越权表.xlsx", XLSX_AGENT, b"sheet-2"),  # result1 不在 SPACES 里
        ("说明.md", "main", b"hello"),
    ]
    async with maker() as s:
        for name, want, body in cases:
            r = await uploads.save_upload(
                s, owner=OWNER, thread_id=THREAD, name=name, src=io.BytesIO(body), spaces=SPACES
            )
            check(f"路由 {name} → {want}", r["target_agent"] == want, f"实际 {r['target_agent']}")

    async with maker() as s:
        r = await uploads.save_upload(
            s, owner=OWNER, thread_id=THREAD, name="说明.md", src=io.BytesIO(b"0123456789"), spaces=SPACES
        )
        check("同名再传是覆盖", r["size"] == 10, f"size={r['size']}")
        rows = (
            await s.execute(text("SELECT count(*) FROM session_object WHERE owner_identity=:o"), {"o": OWNER})
        ).scalar()
        check("台账是 3 行（覆盖不新增）", rows == 3, f"行数 {rows}")

    backend = filesystem_backend(_config())
    listed = await backend.als("/uploads")
    names = sorted(entry["path"] for entry in listed.entries or [])
    check("backend 看见 3 个上传", len(names) == 3, str(names))
    read = await backend.aread("/uploads/说明.md", offset=0, limit=100)
    content = (read.file_data or {}).get("content")
    check("读到覆盖后的内容", content == "0123456789", repr(content))
    hidden = await backend.aread("/uploads/../../etc/passwd", offset=0, limit=10)
    check("越界路径读不到", hidden.file_data is None or bool(hidden.error), str(hidden)[:80])
    files = sorted(p.name for p in upload_store.local_dir(OWNER, THREAD).iterdir())
    check("字节确实落在 data/uploads 下", len(files) == 3, str(files))

    orphans_before = await plan(timedelta(hours=0))
    check(
        "删会话前不算孤儿",
        f"{OWNER}/{THREAD}/" not in orphans_before.orphan_prefixes,
        str(orphans_before.orphan_prefixes),
    )

    async with maker() as s:
        await s.execute(text("DELETE FROM thread WHERE thread_id=:t"), {"t": THREAD})
        await s.commit()
        left = (
            await s.execute(text("SELECT count(*) FROM session_object WHERE owner_identity=:o"), {"o": OWNER})
        ).scalar()
        check("删会话后台账行 CASCADE 掉", left == 0, f"剩 {left} 行")

    check("删会话后字节还在（等 reaper）", upload_store.local_dir(OWNER, THREAD).is_dir())
    orphans = await plan(timedelta(hours=0))
    check(
        "reaper 认出孤儿目录",
        f"{OWNER}/{THREAD}/" in orphans.orphan_prefixes,
        str(orphans.orphan_prefixes),
    )

    await _reset(maker)
    await engine.dispose()
    print(f"\n{'全部通过' if not failures else f'{len(failures)} 项失败: {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
