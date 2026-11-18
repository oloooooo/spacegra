"""多用户并发稳定性验证：20 个用户各自多轮对话，看会不会出错、串号或卡死。

验证什么（跑真服务、真 PG、真 Redis worker、假 LLM）
  1. 成功率：每个 run 都 `success`、每条 SSE 都收到 `end`、没有任何 5xx；
  2. 隔离性：每个用户自己的 thread 里只出现自己的标记，别人说的话一条都不能漏进来；
  3. 不重复执行：假 LLM 的 `chat_completions` 恰好等于 用户数 × 轮数；
  4. 服务端日志里没有连接池枯竭（`couldn't get a connection`）、没有 Traceback；
  5. 顺带给出每轮耗时分布（p50/p95/max），这比"通过"更能看出稳压不稳。

加 `--heartbeat` 会额外每 5s 采一次 `runs.lease_expires_at`，断言租约被心跳持续推后
（需要单次调用长过 `LEASE_DURATION_SECONDS=30`，否则本来就不该续租）。

前置：一个**一次性**的 PG（脚本会 `DROP SCHEMA public CASCADE`，别指向真库）：
    docker run -d --name aegra-e2e-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=aegra -p 15432:5432 postgres:16

跑：
    python scripts/manual/e2e_multi_user.py                     # 20 用户 × 3 轮，每轮约 20s
    python scripts/manual/e2e_multi_user.py --users 20 --turns 5 --tokens 60
    python scripts/manual/e2e_multi_user.py --users 20 --heartbeat --tokens 180   # 心跳：每次调用 90s
    python scripts/manual/e2e_multi_user.py --users 2 --turns 1 --tokens 10 --interval 0.1   # 冒烟

注意：假 LLM 连**非流式**也按同样节奏慢返回 —— 网关走的是 `ainvoke`，
如果它瞬间返回，整个"长对话"2 秒就结束，压不到任何东西。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

API = "http://127.0.0.1:2026"
FAKE = "http://127.0.0.1:2027"
ROOT = Path(__file__).resolve().parents[2]
FAKE_SERVER = Path(__file__).with_name("fake_llm_server.py")

FAIL_PATTERNS = ("couldn't get a connection", "Traceback", "Lease lost", "pool timeout")
SAMPLE_EVERY = 5.0


def build_env(args: argparse.Namespace) -> dict[str, str]:
    return {
        **os.environ,
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": str(args.pg_port),
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "postgres",
        "POSTGRES_DB": "aegra",
        "JWT_SECRET": "e2e-multi-user-secret-0123456789ab",
        "PASSWORD_PEPPER": "e2e-pepper",
        "REDIS_URL": "redis://localhost:6379/5",
        "REDIS_BROKER_ENABLED": "true",
        "OPENAI_API_KEY": "sk-fake",
        "OPENAI_BASE_URL": f"{FAKE}/v1",
        "OPENAI_MODEL": "fake",
        # 控制台是 GBK：日志里的 emoji（✅）会把进程直接报崩
        "PYTHONIOENCODING": "utf-8",
    }


async def wait_http(url: str, *, timeout: float = 120.0) -> None:
    import httpx

    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=3) as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get(url)).status_code < 500:
                    return
            except Exception:  # noqa: BLE001 — 服务还没起来
                pass
            await asyncio.sleep(1.0)
    raise RuntimeError(f"{url} 起不来")


async def reset_schema(dsn: str) -> str:
    """重建空库并 stamp 到 alembic head（main 的迁移链无法从零重放）。"""
    import psycopg
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    from aegra_api.core.database import db_manager
    from aegra_api.core.migrations import find_alembic_ini
    from aegra_api.core.orm import Base

    head = ScriptDirectory.from_config(Config(str(find_alembic_ini()))).get_current_head()
    if not head:
        raise RuntimeError("alembic 没有 head")

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        await conn.execute("CREATE SCHEMA public")

    await db_manager.initialize()  # 建 langgraph 的 checkpointer / store 表
    async with db_manager.get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            )
        )
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v) ON CONFLICT DO NOTHING"), {"v": head}
        )
    await db_manager.close()
    return head


async def create_accounts(client, users: int) -> list[tuple[str, str, str]]:
    """建超管 + users-1 个普通账号并逐个登录，返回 [(identity, token, marker)]。"""
    first = (await client.get(f"{API}/custom/first-run")).json()
    if not first.get("first_run"):
        raise RuntimeError("库不是空的：first_run=false，脚本不做增量")

    r = await client.post(
        f"{API}/custom/bootstrap",
        json={"identity": "root", "display_name": "root", "password": "root12345"},
    )
    r.raise_for_status()
    admin_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    accounts = [("root", "root12345")]
    for i in range(1, users):
        ident = f"u{i:02d}"
        r = await client.post(
            f"{API}/custom/users",
            json={"identity": ident, "display_name": ident, "password": "pass12345"},
            headers=admin_headers,
        )
        r.raise_for_status()
        accounts.append((ident, "pass12345"))

    out: list[tuple[str, str, str]] = []
    for ident, password in accounts:
        r = await client.post(f"{API}/custom/login", json={"identity": ident, "password": password})
        r.raise_for_status()
        out.append((ident, r.json()["access_token"], f"MARK-{ident}-{int(time.time()) % 100000}"))
    return out


async def one_user(client, ident: str, token: str, marker: str, index: int, turns: int) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    thread_id = f"mu-{index:02d}-{int(time.time())}"
    r = await client.post(f"{API}/threads", json={"thread_id": thread_id}, headers=headers)
    r.raise_for_status()

    res: dict = {"identity": ident, "thread_id": thread_id, "marker": marker, "durations": [], "errors": [], "ends": 0}
    for turn in range(turns):
        body = {
            "assistant_id": "supervisor",
            "input": {"messages": [{"role": "user", "content": f"{marker} 第{turn}轮：讲讲这块知识。"}]},
            "stream": True,
        }
        started = time.monotonic()
        try:
            async with client.stream(
                "POST", f"{API}/threads/{thread_id}/runs/stream", json=body, headers=headers, timeout=None
            ) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode("utf-8", "replace")[:200]
                    res["errors"].append(f"HTTP {resp.status_code}: {text}")
                    continue
                async for line in resp.aiter_lines():
                    if line.startswith("event:") and line.split(":", 1)[1].strip() in {"end", "error"}:
                        res["ends"] += 1
                        break
        except Exception as exc:  # noqa: BLE001 — 单个用户失败要记进报告，不打断其他用户
            res["errors"].append(f"{type(exc).__name__}: {exc}")
        res["durations"].append(time.monotonic() - started)
    return res


async def thread_contents(client, thread_id: str, headers: dict) -> list[str]:
    r = await client.get(f"{API}/threads/{thread_id}/state", headers=headers)
    r.raise_for_status()
    values = r.json().get("values") or {}
    return [str(m.get("content")) for m in (values.get("messages") or [])]


async def snapshot_leases(dsn: str, seen: dict[str, dict]) -> None:
    """采一次 runs 表，累积每个 run 的租约值 / claimed_by / 状态。"""
    import psycopg

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        cur = await conn.execute("SELECT run_id, status, claimed_by, lease_expires_at FROM runs")
        for run_id, status, claimed_by, lease in await cur.fetchall():
            slot = seen.setdefault(run_id, {"leases": set(), "claimed_by": set(), "statuses": set()})
            slot["leases"].add(lease)
            slot["claimed_by"].add(claimed_by)
            slot["statuses"].add(status)


async def sample_leases(dsn: str, stop: asyncio.Event, seen: dict[str, dict]) -> None:
    while not stop.is_set():
        await snapshot_leases(dsn, seen)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SAMPLE_EVERY)
        except TimeoutError:
            continue


async def main() -> int:
    parser = argparse.ArgumentParser(description="多用户并发稳定性验证")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--turns", type=int, default=3, help="每个用户几轮对话")
    parser.add_argument("--tokens", type=int, default=40, help="每次模型调用模拟多慢（片数）")
    parser.add_argument("--interval", type=float, default=0.5, help="每片间隔秒数")
    parser.add_argument("--pg-port", type=int, default=15432)
    parser.add_argument("--heartbeat", action="store_true", help="额外观测并断言租约心跳")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.environ.update(build_env(args))

    import httpx
    import redis.asyncio as aioredis

    dsn = f"postgresql://postgres:postgres@127.0.0.1:{args.pg_port}/aegra"
    await aioredis.from_url("redis://localhost:6379/5").flushdb()
    head = await reset_schema(dsn)
    print(f"库已重建，stamp {head}")

    log = tempfile.NamedTemporaryFile(prefix="aegra-multi-user-", suffix=".log", delete=False)  # noqa: SIM115
    app_log = Path(log.name)
    procs: list[subprocess.Popen] = []

    def spawn(cmd: list[str], *, stdout: object = None) -> None:
        procs.append(subprocess.Popen(cmd, cwd=ROOT, env=os.environ.copy(), stdout=stdout))  # noqa: S603

    try:
        spawn(
            [
                sys.executable,
                str(FAKE_SERVER),
                "--port",
                "2027",
                "--tokens",
                str(args.tokens),
                "--interval",
                str(args.interval),
            ],
            stdout=log,
        )
        await wait_http(f"{FAKE}/v1/models")
        spawn(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "aegra_api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "2026",
                "--loop",
                "asyncio:SelectorEventLoop",
                "--log-level",
                "warning",
            ],
            stdout=log,
        )
        await wait_http(f"{API}/custom/first-run", timeout=180)

        per_call = args.tokens * args.interval
        print(f"{args.users} 用户 × {args.turns} 轮，每次模型调用约 {per_call:.0f}s，用户之间并发、轮次之间串行")

        started = time.time()
        async with httpx.AsyncClient(timeout=60) as client:
            accounts = await create_accounts(client, args.users)
            leases: dict[str, dict] = {}
            stop = asyncio.Event()
            sampler = asyncio.create_task(sample_leases(dsn, stop, leases)) if args.heartbeat else None
            results = await asyncio.gather(
                *(
                    one_user(client, ident, token, marker, i, args.turns)
                    for i, (ident, token, marker) in enumerate(accounts)
                )
            )
            elapsed = time.time() - started
            if sampler is not None:
                stop.set()
                await sampler
                await snapshot_leases(dsn, leases)  # 收尾再采一次，拿终态

            # 隔离性：每个 thread 里只能出现自己的标记
            leaks: list[str] = []
            for (ident, token, marker), res in zip(accounts, results, strict=True):
                contents = " ".join(
                    await thread_contents(client, res["thread_id"], {"Authorization": f"Bearer {token}"})
                )
                if marker not in contents:
                    leaks.append(f"{ident}: 自己的标记没出现在自己的 thread 里")
                for other_ident, _t, other in accounts:
                    if other_ident != ident and other in contents:
                        leaks.append(f"{ident} 的 thread 里出现了 {other_ident} 的标记")

            # 终态 + 重复执行
            import psycopg

            async with await psycopg.AsyncConnection.connect(dsn) as conn:
                cur = await conn.execute("SELECT status, count(*) FROM runs GROUP BY status")
                statuses = dict(await cur.fetchall())
            stats = (await client.get(f"{FAKE}/__stats")).json()
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()

    # ---- 报告 ----
    durations = sorted(d for res in results for d in res["durations"])
    total_runs = args.users * args.turns
    problems: list[str] = []
    for res in results:
        problems.extend(f"{res['identity']}: {err}" for err in res["errors"])
        if res["ends"] != args.turns:
            problems.append(f"{res['identity']}: 只收到 {res['ends']}/{args.turns} 个 end 事件")
    if statuses != {"success": total_runs}:
        problems.append(f"run 终态分布不对：{statuses}，预期 success={total_runs}")
    if stats["chat_completions"] != total_runs:
        problems.append(f"模型被调用 {stats['chat_completions']} 次，预期 {total_runs}（多于预期=重复执行）")
    problems.extend(leaks)

    log_text = app_log.read_text(encoding="utf-8", errors="replace")
    hits = [pattern for pattern in FAIL_PATTERNS if pattern in log_text]
    if hits:
        problems.append(f"服务端日志里有异常特征：{hits}（{app_log}）")

    if args.heartbeat:
        for run_id, slot in sorted(leases.items()):
            distinct = {lease for lease in slot["leases"] if lease is not None}
            claimed = {worker for worker in slot["claimed_by"] if worker}
            if len(distinct) < 3:
                problems.append(f"{run_id}: 只采到 {len(distinct)} 个租约值，心跳没续上")
            if len(claimed) != 1:
                problems.append(f"{run_id}: claimed_by 不稳定 {claimed}")
        counts = [len({lease for lease in s["leases"] if lease is not None}) for s in leases.values()]
        if counts:
            print(f"租约：单 run 采样到 {min(counts)}~{max(counts)} 个不同值（心跳 10s / 租约 30s）")

    def pct(values: list[float], q: float) -> float:
        return values[min(len(values) - 1, int(len(values) * q))] if values else 0.0

    print(f"\n{total_runs} 个 run，总耗时 {elapsed:.0f}s；终态 {statuses}；模型调用 {stats['chat_completions']} 次")
    print(
        f"单轮耗时 p50 {pct(durations, 0.5):.1f}s / p95 {pct(durations, 0.95):.1f}s / max {durations[-1] if durations else 0:.1f}s"
    )
    print(f"服务端日志：{app_log}")
    if problems:
        print("\n失败：")
        for item in problems[:20]:
            print("  -", item)
        return 1
    print("\n通过：全部成功、无串号、无重复执行、服务端无连接池/堆栈异常")
    return 0


# psycopg 的异步连接在 Windows 上必须跑 selector 循环（3.14 起 set_event_loop_policy 已失效，只能走 loop_factory）
if __name__ == "__main__":
    if sys.platform == "win32":
        raise SystemExit(asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop))
    raise SystemExit(asyncio.run(main()))
