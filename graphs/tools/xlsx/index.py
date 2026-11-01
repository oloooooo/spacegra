"""xlsx → SQLite 索引（ADR-0015 §5）：建一次，之后四个工具都查它。

为什么不是"每次 read_excel"：80k×10×3 的表一次全读 41.7 s / 129 MB，而建好索引后
`WHERE … LIMIT 200` 是 2 ms。慢的不是查询，是重复解析。

安全边界都在 `open_ro()` 里：`mode=ro&immutable=1` + authorizer 白名单 +
`set_progress_handler` 限时。`xlsx_query` 的 `where` 是直拼进 SQL 的，靠这三样兜住：
最坏情况只能读到同一张索引表里的行，写不了、ATTACH 不了、也炸不了分钟级笛卡尔积。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pandas as pd

from shared import config

# 改 build 产物结构就 +1：旧索引会因 key 变化而自然失效（否则永远读到旧结构）
BUILD = 1

_MAX_ROWS = 200
_MAX_HITS = 100
_MAX_VALUE_LEN = 80
_QUERY_BUDGET_MS = int(config.get("xlsx.query_budget_ms"))
# 结构性硬闸：不依赖字节大小（同样 10 MB，可以是 30 万 cell，也可以是 500 万）
_MAX_CELLS = int(config.get("xlsx.max_cells"))
# 本机缓存（索引与字节）的保留天数
_CACHE_TTL_DAYS = int(config.get("xlsx.cache_ttl_days"))

_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}
# 只放常用函数；放开整个 SQLITE_FUNCTION 等于放开 zeroblob(1e8)。
# 注意：authorizer 对 SQLITE_FUNCTION 把函数名放在**第三个**位置参数（arg2），arg1 是 None。
_ALLOWED_FUNCS = {
    "like",
    "glob",
    "lower",
    "upper",
    "length",
    "abs",
    "round",
    "coalesce",
    "ifnull",
    "substr",
    "trim",
    "count",
    "sum",
    "total",
    "avg",
    "min",
    "max",
}


# ---------- 建索引 ----------


def build(xlsx: Path, dest: Path, header: int) -> None:
    """把 xlsx 的每个 sheet 落成一张表，外加 meta / col_meta 两张小表。"""
    sheets = pd.read_excel(xlsx, sheet_name=None, header=header)
    conn = sqlite3.connect(dest)
    try:
        conn.execute("PRAGMA journal_mode=OFF")  # 临时文件，崩了重建
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("CREATE TABLE meta(sheet_idx INTEGER, name TEXT, nrows INTEGER)")
        conn.execute("CREATE TABLE col_meta(sheet_idx INTEGER, col_idx INTEGER, name TEXT, dtype TEXT)")
        for i, (name, df) in enumerate(sheets.items()):
            df.to_sql(f"s{i}", conn, index=False)
            conn.execute("INSERT INTO meta VALUES (?,?,?)", (i, str(name), len(df)))
            conn.executemany(
                "INSERT INTO col_meta VALUES (?,?,?,?)",
                [(i, j, str(c), str(df[c].dtype)) for j, c in enumerate(df.columns)],
            )
        conn.commit()
    finally:
        conn.close()


def _cell_count(xlsx: Path) -> int:
    """用 openpyxl read_only 探行列数（实测 27 ms / 0.9 MB，不会把表读进内存）。"""
    from openpyxl import load_workbook

    wb = load_workbook(xlsx, read_only=True)
    try:
        return sum((ws.max_row or 0) * (ws.max_column or 0) for ws in wb.worksheets)
    finally:
        wb.close()


def cell_count(xlsx: Path) -> int:
    """表规模（行×列，按 sheet 累加）。工具层用它判断要不要建 Parquet 派生缓存。"""
    return _cell_count(xlsx)


def ensure(xlsx: Path, cache_dir: Path, cache_key: str, header: int) -> Path:
    """返回可用的索引路径；没有就建（先写临时文件再原子替换，避免读到半个文件）。

    建之前先探 cell 数：一张“能上传但处理不了”的表，在这里拒掉比让 agent 等 40 秒再撞超时好。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    _sweep_cache(cache_dir)
    dest = cache_dir / f"{cache_key}-h{header}-v{BUILD}.sqlite"
    if dest.is_file():
        return dest
    cells = _cell_count(xlsx)
    if cells > _MAX_CELLS:
        raise ValueError(f"表太大：约 {cells} 个单元格，超过上限 {_MAX_CELLS}（请拆表或改存 csv）")
    tmp = dest.with_suffix(".sqlite.tmp")
    build(xlsx, tmp, header)
    os.replace(tmp, dest)  # 原子：并发构建时最后落地的赢，内容语义相同
    return dest


_swept = False


def _sweep_cache(cache_dir: Path) -> None:
    """每个进程扫一次：删掉超过 `xlsx.cache_ttl_days` 的缓存（删了按需重建）。"""
    global _swept
    if _swept:
        return
    _swept = True
    cutoff = time.time() - _CACHE_TTL_DAYS * 86400
    for path in cache_dir.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:  # noqa: PERF203 —— 单个文件删不掉不该影响其它
            continue


# ---------- 只读打开 ----------


def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    conn.set_authorizer(_authorizer)
    deadline = time.monotonic() + _QUERY_BUDGET_MS / 1000
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 20_000)
    return conn


def _authorizer(action: int, arg1: Any, arg2: Any, _db: Any, _src: Any) -> int:
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        name = arg2 if arg2 is not None else arg1
        if str(name).lower() in _ALLOWED_FUNCS:
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


# ---------- 结构 ----------


def sheets(conn: sqlite3.Connection) -> list[tuple[int, str, int]]:
    return [(i, n, r) for i, n, r in conn.execute("SELECT sheet_idx, name, nrows FROM meta ORDER BY sheet_idx")]


def columns(conn: sqlite3.Connection, sheet_idx: int) -> list[tuple[str, str]]:
    return [
        (n, d)
        for n, d in conn.execute("SELECT name, dtype FROM col_meta WHERE sheet_idx=? ORDER BY col_idx", (sheet_idx,))
    ]


def _pick(sheets_: list[tuple[int, str, int]], sheet: str | None) -> tuple[int, str]:
    """选 sheet：指定了就用它（不存在报错），否则第一个。"""
    if sheet is not None:
        for i, n, _ in sheets_:
            if n == sheet:
                return i, n
        raise ValueError(f"sheet 不存在：{sheet}，可用：{[n for _, n, _ in sheets_]}")
    if not sheets_:
        raise ValueError("这个 xlsx 里没有任何 sheet")
    return sheets_[0][0], sheets_[0][1]


def _q(name: str) -> str:
    """列名加双引号：`数量(个)`、`价 格` 这类不加就是语法错/找不到列。"""
    return '"' + name.replace('"', '""') + '"'


# ---------- 四个工具 ----------


def info(conn: sqlite3.Connection, file_name: str, sheet: str | None) -> str:
    sheets_ = sheets(conn)
    out: dict[str, Any] = {
        "file": file_name,
        "sheets": {n: {"rows": r, "columns": [c for c, _ in columns(conn, i)]} for i, n, r in sheets_},
    }
    idx, preview = _pick(sheets_, sheet)
    names = [c for c, _ in columns(conn, idx)]
    rows = conn.execute(f"SELECT * FROM s{idx} LIMIT 3").fetchall()
    out["preview"] = {
        "sheet": preview,
        "head": [{names[j]: (None if v is None else v) for j, v in enumerate(row)} for row in rows],
    }
    return json.dumps(out, ensure_ascii=False, default=str)


def query(
    conn: sqlite3.Connection,
    sheet: str | None,
    columns_: list[str] | None,
    where: str | None,
    limit: int,
    offset: int,
) -> str:
    idx, _ = _pick(sheets(conn), sheet)
    available = [c for c, _ in columns(conn, idx)]
    if columns_:
        missing = [c for c in columns_ if c not in available]
        if missing:
            raise ValueError(f"列不存在：{missing}，可用列：{available}")
        picked = list(columns_)
    else:
        picked = available
    clause = f" WHERE {where}" if where else ""
    total = int(conn.execute(f"SELECT COUNT(*) FROM s{idx}{clause}").fetchone()[0])
    # limit/offset 是 agent 传的，负数会经切片漏出全表（limit=-1 -> 全量），先钳正
    limit = max(1, min(int(limit), _MAX_ROWS))
    offset = max(0, int(offset))
    cols = ", ".join(_q(c) for c in picked)
    rows = conn.execute(f"SELECT {cols} FROM s{idx}{clause} LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    df = pd.DataFrame(rows, columns=picked)
    head = f"匹配 {total} 行，返回第 {offset + 1}-{offset + len(df)} 行（列：{[str(c) for c in df.columns]}）"
    return head + "\n" + df.to_csv(index=False)


def search(
    conn: sqlite3.Connection,
    keyword: str,
    sheet: str | None,
    header: int,
    limit: int,
) -> str:
    if not keyword:
        raise ValueError("keyword 不能为空")
    sheets_ = sheets(conn)
    if sheet is not None:
        sheets_ = [s for s in sheets_ if s[1] == sheet]
        if not sheets_:
            raise ValueError(f"sheet 不存在：{sheet}，可用：{[n for _, n, _ in sheets(conn)]}")
    pattern = f"%{_escape_like(keyword)}%"
    hits: list[str] = []
    total = 0
    # 逐**单元格**命中：一行里两列都命中要出两条，且要带列名（与 pandas 版一致）。
    # 所以按列 UNION ALL，而不是一行一次 WHERE。
    for idx, name, _ in sheets_:
        for col, _dtype in columns(conn, idx):
            sql = f"SELECT rowid, {_q(col)}, CAST({_q(col)} AS TEXT) FROM s{idx} WHERE {_q(col)} LIKE ? ESCAPE '\\'"
            for rowid, colname, value in conn.execute(sql, (pattern,)):
                total += 1
                if len(hits) < min(limit, _MAX_HITS):
                    hits.append(f"{name} | Excel第{rowid + header + 1}行 | {colname} | {str(value)[:_MAX_VALUE_LEN]}")
    if not hits:
        return f"未找到包含 {keyword!r} 的单元格"
    truncated = f"（仅显示前 {len(hits)} 条，缩小关键词或改用 xlsx_query 按列取数）" if total > len(hits) else ""
    return f"命中 {total} 处{truncated}（sheet | 行 | 列 | 值）：\n" + "\n".join(hits)


def profile(conn: sqlite3.Connection, sheet: str | None) -> str:
    idx, _ = _pick(sheets(conn), sheet)
    cols: dict[str, Any] = {}
    for col, dtype in columns(conn, idx):
        q = _q(col)
        nulls = int(conn.execute(f"SELECT COUNT(*) - COUNT({q}) FROM s{idx}").fetchone()[0])
        unique = int(conn.execute(f"SELECT COUNT(DISTINCT {q}) FROM s{idx}").fetchone()[0])
        item: dict[str, Any] = {"dtype": dtype, "nulls": nulls, "unique": unique}
        if dtype.startswith(("int", "Int", "float", "Float", "uint", "UInt")):
            lo, hi, mean = conn.execute(f"SELECT MIN({q}), MAX({q}), AVG({q}) FROM s{idx}").fetchone()
            item["min"] = lo
            item["max"] = hi
            item["mean"] = round(float(mean), 4) if mean is not None else None
        else:
            samples = [r[0] for r in conn.execute(f"SELECT DISTINCT {q} FROM s{idx} WHERE {q} IS NOT NULL LIMIT 5")]
            item["sample"] = samples
        cols[col] = item
    names = [c for c, _ in columns(conn, idx)]
    counts = ", ".join(_q(c) for c in names) if names else "''"
    duplicated = int(
        conn.execute(
            f"SELECT COALESCE(SUM(c - 1), 0) FROM (SELECT COUNT(*) c FROM s{idx} GROUP BY {counts} HAVING c > 1)"
        ).fetchone()[0]
    )
    rows = int(conn.execute(f"SELECT COUNT(*) FROM s{idx}").fetchone()[0])
    out = {"rows": rows, "duplicate_rows": duplicated, "columns": cols}
    return json.dumps(out, ensure_ascii=False, default=str)


def _escape_like(text: str) -> str:
    """`_` 与 `%` 是 LIKE 的通配符，用户搜的是字面量。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
