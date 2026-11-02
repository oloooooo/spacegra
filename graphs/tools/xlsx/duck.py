"""xlsx → Parquet 派生缓存 + DuckDB 查询（ADR-0018 §3、§6）。

为什么要有这一层
----------------
xlsx 是 zip 压缩的 XML，每次查都要重新解压解析：**80k×10×3** 的表演上一次全读
**41.7 s / 129 MB**，解析一次存成列式文件后 `WHERE … LIMIT 200` 是 **2 ms**。

早先的实现用 SQLite 建索引（本分支已删除），三个结构性问题：索引键是「路径 +
mtime + size」，跨机器不通用；SQLite 依赖文件锁语义，`cache_dir` 一旦指向共享卷
就不再成立；只剩一种执行器，跨表/跨 sheet 的 join 要手工搬数据。

这一层改用 parquet 当中间产物：**按内容哈希（sha256）命名** → 同一个文件在任何
实例、任何机器上解析结果都一样，可以直接共享；查询用 DuckDB（`read_parquet`，
列裁剪 + 谓词下推），跨 sheet/跨表就是一句 SQL。

缓存介质（`xlsx.cache_mode`）
-----------------------------
``local`` 只落本机缓存目录（单实例部署，最省事）；
``s3``    派生文件写对象存储，查询时由 DuckDB 直接 ``read_parquet('s3://…')`` 边读边算——
**不依赖本地缓存目录**，多实例/弹性伸缩时任何机器都不需要预置或共享磁盘（本地那份
只是建缓存时的副产物，命中与否都不影响查询）。
没有 ``off``：不建缓存意味着每次查询重新解析（80k 行级 40 s），这不是一个
能交给运维去切的旋钮。

多实例并发的两条护栏：派生文件按内容哈希命名（内容确定，谁写都一样），落盘用
`store.write_atomic`（临时文件 + rename，读者看不到半截 Parquet）。清单
（manifest）最后写，看到清单就意味着 parquet 已经完整。

`header`（表头在第 N 行）是缓存键的一部分：`<sha256>/h<header>/`。不同 header 各存
一份，常见情况只有 h0 一份。这件事以前由 SQLite 路径的「跳行」逻辑处理，现在
`pd.read_excel(header=...)` 在解析时一次做掉，四个工具不再分叉。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from shared import config
from shared.session import blobs

from . import store

_MAX_ROWS = 200
_MAX_HITS = 100
_MAX_VALUE_LEN = 80
_MAX_COLUMNS = 40
_BUDGET_MS = int(config.get("xlsx.query_budget_ms"))
_MAX_CELLS = int(config.get("xlsx.max_cells"))
_SHEET_NAME_MAX = 120
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_FORBIDDEN_SQL = re.compile(
    r"(?<![\w.])(attach|detach|pragma|create|insert|update|delete|drop|alter|copy|export|import|install|load|call|set"
    r"|select|read_csv|read_csv_auto|read_parquet|parquet_scan|read_json|read_json_auto|read_text|read_blob"
    r"|glob|sniff_csv)\b",
    re.IGNORECASE,
)
# 谓词的语法树白名单：只放行列引用、字面量与比较/逻辑运算（`_check_predicate`）。
_ALLOWED_NODES = frozenset(
    {
        "SELECT_NODE",
        "EMPTY",
        "ORDER_MODIFIER",
        "TYPE",
        "COLUMN_REF",
        "STAR",
        "VALUE_CONSTANT",
        "COMPARE_EQUAL",
        "COMPARE_NOTEQUAL",
        "COMPARE_LESSTHAN",
        "COMPARE_GREATERTHAN",
        "COMPARE_LESSTHANOREQUALTO",
        "COMPARE_GREATERTHANOREQUALTO",
        "COMPARE_DISTINCT_FROM",
        "COMPARE_NOT_DISTINCT_FROM",
        "COMPARE_IN",
        "COMPARE_BETWEEN",
        "CONJUNCTION_AND",
        "CONJUNCTION_OR",
        "OPERATOR_IS_NULL",
        "OPERATOR_IS_NOT_NULL",
        "OPERATOR_NOT",
        "OPERATOR_CAST",
        "OPERATOR_COALESCE",
        "FUNCTION",
    }
)
# LIKE 在语法树里是 `~~`，拼接是 `||`，算术也是 FUNCTION：一律按名字放行。
_ALLOWED_FUNCTIONS = frozenset(
    {
        "+",
        "-",
        "*",
        "/",
        "%",
        "||",
        "~~",
        "!~~",
        "~~*",
        "!~~*",
        "lower",
        "upper",
        "length",
        "trim",
        "ltrim",
        "rtrim",
        "abs",
        "round",
        "ceil",
        "floor",
        "substr",
        "substring",
        "strftime",
        "date_trunc",
    }
)


# ---------- 配置与路径 ----------


def cache_mode() -> str:
    """``local`` | ``s3``；s3 未配置对象存储时自动退回 local（配置写错不该全线报错）。"""
    mode = str(config.get("xlsx.cache_mode")).strip().lower() or "local"
    if mode != "s3":
        return "local"
    return "s3" if blobs.configured() else "local"


def sql_policy() -> str:
    """``strict`` = 语法树白名单（默认）；``free`` = 只拦多条语句/注释，谓词原样交给 DuckDB。"""
    return str(config.get("xlsx.sql_policy")).strip().lower() or "strict"


def _derived_dir() -> Path:
    return store.cache_dir() / "derived"


def _sheet_file(sheet: str) -> str:
    """sheet 名 → 文件名。只留安全字符：sheet 名来自上传的表格，不能决定路径。"""
    cleaned = _UNSAFE.sub("_", str(sheet)).strip(" .")[:_SHEET_NAME_MAX]
    return f"{cleaned or 'sheet'}.parquet"


def _relative(sha256: str, header: int) -> str:
    """``<sha256>/h<header>``：表头行号进路径，不同 header 各存一份。"""
    return f"{sha256}/h{int(header)}"


def store_path(sha256: str, sheet: str, header: int = 0) -> Path:
    """本地派生文件位置：``<cache_dir>/derived/<sha256>/h<header>/<sheet>.parquet``。"""
    return _derived_dir() / _relative(sha256, header) / _sheet_file(sheet)


def remote_key(sha256: str, sheet: str, header: int = 0) -> str:
    """对象存储里的位置：``<xlsx.cache_prefix>/<sha256>/h<header>/<sheet>.parquet``。"""
    return f"{str(config.get('xlsx.cache_prefix')).strip('/')}/{_relative(sha256, header)}/{_sheet_file(sheet)}"


def _manifest_path(sha256: str, header: int = 0) -> Path:
    return _derived_dir() / _relative(sha256, header) / "manifest.json"


def _remote_manifest_key(sha256: str, header: int = 0) -> str:
    return f"{str(config.get('xlsx.cache_prefix')).strip('/')}/{_relative(sha256, header)}/manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------- 建派生缓存 ----------


def _connect():  # noqa: ANN202 —— duckdb 的类型在可选依赖里
    """内存库 + 锁配置；`local` 模式再关掉外部访问、只留派生目录。

    `where` 是模型写的谓词，是最外层的注入面。护栏分两层：`_check_predicate` 先做
    文本检查、再按语法树白名单把关（子查询/表函数/陌生函数一律拒）；连接层在
    `local` 模式下把 `enable_external_access` 关掉，`read_text`/`glob`/`read_csv`
    即使被调进来也读不到禁止目录。

    `s3` 模式下外部访问必须开着（DuckDB 要直读对象存储），此时本地/远端文件访问
    只能靠语法树闸门拦住——这是刻意的取舍：多主机不依赖缓存目录，代价是注入面
    全部压在谓词校验上。
    """
    import duckdb

    con = duckdb.connect(":memory:")
    if cache_mode() == "s3":
        _configure_s3(con)
    else:
        # 顺序不能换：``enable_external_access=false`` 之后 allowed_directories 就改不了了，
        # 而关掉外部访问后它仍是唯一还能读的目录（ADR-0018 §10）。
        con.execute(f"SET allowed_directories = [{_literal(_derived_dir().as_posix())}]")
        con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


def _configure_s3(con: Any) -> None:
    """让 DuckDB 直接读对象存储，凭证与 boto3 共用一份。

    SeaweedFS/MinIO 一律 path style（``bucket.endpoint`` 解析不了，和 boto3 那边同一个
    理由）。httpfs 扩展首次用会自动下载，内网需预置。
    """
    endpoint = str(config.get("s3.endpoint") or "").strip()
    if not endpoint:
        return
    parsed = urlsplit(endpoint if "//" in endpoint else f"https://{endpoint}")
    con.execute(f"SET s3_endpoint = {_literal(parsed.netloc or parsed.path)}")
    con.execute(f"SET s3_use_ssl = {'true' if parsed.scheme == 'https' else 'false'}")
    con.execute("SET s3_url_style = 'path'")
    con.execute(f"SET s3_region = {_literal(str(config.get('s3.region') or '').strip() or 'us-east-1')}")
    access_key, secret_key = blobs.credentials()
    con.execute(f"SET s3_access_key_id = {_literal(access_key)}")
    con.execute(f"SET s3_secret_access_key = {_literal(secret_key)}")


def _frames(source: Path, header: int) -> dict[str, Any]:
    import pandas as pd

    return pd.read_excel(source, sheet_name=None, header=header, engine="openpyxl")


async def ensure(source: Path, header: int = 0) -> str | None:
    """算内容哈希 → 读/建派生缓存，返回 sha256（不可用则 None）。

    先看清单再看数据：清单命中就不用解析，跨实例也能直接复用（s3 模式下会从
    对象存储把 parquet 拉回本地）。
    """
    import asyncio

    sha256 = await asyncio.to_thread(sha256_file, source)
    if reload_manifest(sha256, header) is not None:
        return sha256
    manifest = await asyncio.to_thread(build_derived, sha256, source, header)
    return sha256 if manifest is not None else None


def _clean_columns(columns: Any) -> list[str]:
    """列名转字符串 + 去重。重复列名（空表头常见）会让 `CREATE TABLE` 直接失败。"""
    out: list[str] = []
    seen: dict[str, int] = {}
    for raw in columns:
        name = str(raw).strip() or "列"
        count = seen.get(name, 0) + 1
        seen[name] = count
        out.append(name if count == 1 else f"{name}_{count}")
    return out


def build_derived(sha256: str, source: Path, header: int = 0) -> dict[str, Any] | None:
    """把 xlsx 的每个 sheet 转成一份 Parquet（同步，跑在线程池里）。

    返回清单；数据规模超过 `xlsx.max_cells` 时返回 None——那种表任何查询都会打满
    CPU，调用方应当直接回一句"表太大"，而不是先落一份几 GB 的中间产物。
    """
    frames = _frames(source, header)
    con = _connect()
    try:
        sheets: list[dict[str, Any]] = []
        cells = 0
        for name, frame in frames.items():
            frame = frame.dropna(axis=0, how="all").dropna(axis=1, how="all")
            if frame.empty:
                continue
            cells += int(frame.shape[0]) * int(frame.shape[1])
            if cells > _MAX_CELLS:
                return None
            frame = frame.copy()
            frame.columns = _clean_columns(frame.columns)  # type: ignore[assignment]
            target = store_path(sha256, name, header)
            con.register("staging", frame)
            try:
                tmp = target.with_suffix(".parquet.tmp")
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.unlink(missing_ok=True)
                con.execute(f"COPY staging TO {_literal(tmp.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)")
                data = tmp.read_bytes()
            finally:
                con.unregister("staging")
                tmp.unlink(missing_ok=True)
            store.write_atomic(target, data)
            sheets.append(
                {
                    "name": str(name),
                    "rows": int(frame.shape[0]),
                    "columns": [str(c) for c in frame.columns],
                    "bytes": len(data),
                }
            )
    finally:
        con.close()

    manifest = {
        "version": 2,
        "sha256": sha256,
        "header": int(header),
        "engine": "duckdb",
        "sheets": sheets,
        "rows": sum(s["rows"] for s in sheets),
        "built_at": int(time.time()),
    }
    store.write_atomic(_manifest_path(sha256, header), json.dumps(manifest, ensure_ascii=False).encode())
    if cache_mode() == "s3":
        for sheet in sheets:
            source_file = store_path(sha256, sheet["name"], header)
            blobs.put_bytes(remote_key(sha256, sheet["name"], header), source_file.read_bytes())
        blobs.put_bytes(_remote_manifest_key(sha256, header), json.dumps(manifest, ensure_ascii=False).encode())
    _enforce_size_limit()
    return manifest


def reload_manifest(sha256: str, header: int = 0) -> dict[str, Any] | None:
    """读清单。本地没有就看对象存储（多实例共享）；远端也没有就返回 None。"""
    path = _manifest_path(sha256, header)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    if cache_mode() != "s3":
        return None
    raw = blobs.get_or_none(_remote_manifest_key(sha256, header))
    if raw is None:
        return None
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    store.write_atomic(path, raw)
    return manifest


def prune_remote(older_than_days: float, *, dry_run: bool = False) -> tuple[int, int]:
    """清掉对象存储里过期的派生缓存，返回 `(对象数, 字节数)`。

    派生文件是内容寻址、可重建的：删了只会让下次查询多解析一次，不会丢数据 ——
    所以能用"多久没被重写"当 TTL，不用像 uploads 那样对着台账留活。
    """
    prefix = str(config.get("xlsx.cache_prefix")).strip("/") + "/"
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    s3 = blobs.client()
    bucket = blobs.bucket()
    victims: list[str] = []
    total = 0
    token: str | None = None
    while True:
        kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            if obj.get("LastModified") and obj["LastModified"] < cutoff:
                victims.append(obj["Key"])
                total += int(obj.get("Size") or 0)
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    if not dry_run:
        for key in victims:
            blobs.delete(key)
    return len(victims), total


def _enforce_size_limit() -> None:
    """本机缓存超过预算就按"最久未改动"淘汰，避免把磁盘吃满。"""
    limit_mb = int(config.get("xlsx.cache_local_max_mb"))
    if limit_mb <= 0:
        return
    root = _derived_dir()
    if not root.is_dir():
        return
    files = sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    limit = limit_mb * 1024 * 1024
    for path in files:
        if total <= limit:
            break
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size


# ---------- 取本地 parquet ----------


def local_parquet(sha256: str, sheet: str, header: int = 0) -> Path | str | None:
    """派生 parquet 的读取位置：`s3` 模式给 `s3://` 地址，本地模式给本机路径。

    s3 模式不再拉回本地——多主机共享同一份对象，不必预置/同步缓存目录（也顺带
    去掉了「本地副本比远端旧」的问题）；本地那份只是建缓存时的副产物。
    """
    if cache_mode() == "s3":
        return f"s3://{blobs.bucket()}/{remote_key(sha256, sheet, header)}"
    path = store_path(sha256, sheet, header)
    return path if path.is_file() else None


# ---------- SQL ----------


def _literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _ident(name: str) -> str:
    """列名标识符：双引号包住、内部双引号翻倍（表头来自 xlsx，不可信）。"""
    return '"' + str(name).replace('"', '""') + '"'


def _source(path: Path | str) -> str:
    """`read_parquet()` 的实参：本地路径或 `s3://` 地址都走同一条路。"""
    return f"read_parquet({_literal(str(path))})"


def _budget(con: Any) -> threading.Timer:
    """查询预算：到点 `interrupt()`。DuckDB 是 C++ 循环，只能靠打断。"""
    timer = threading.Timer(_BUDGET_MS / 1000.0, con.interrupt)
    timer.daemon = True
    timer.start()
    return timer


def _rows(con: Any, sql: str) -> list[tuple[Any, ...]]:
    timer = _budget(con)
    try:
        return con.execute(sql).fetchall()
    finally:
        timer.cancel()


def _columns(con: Any, path: Path) -> list[str]:
    rows = _rows(con, f"DESCRIBE SELECT * FROM {_source(path)}")
    return [str(r[0]) for r in rows]


def _check_where(where: str) -> str:
    """`where` 是模型写的 SQL 谓词：禁掉多语句、注释与 DDL/扩展加载。

    注释符必禁：``1=1 --`` 会把后面的 ``LIMIT/OFFSET`` 一并注释掉。
    """
    text = where.strip().rstrip(";").strip()
    if ";" in text or "--" in text or "/*" in text or _FORBIDDEN_SQL.search(text):
        raise ValueError("where 只能是筛选谓词，不能包含分号、注释或 DDL/多条语句")
    return text


def _check_predicate(con: Any, where: str) -> str:
    """文本闸门 + 语法树白名单：不许子查询、表函数和白名单外的函数。

    文本黑名单挡不住 ``EXISTS (FROM query('FROM read_text(…'))`` 这种拼法；这里让
    DuckDB 自己解析成语法树，按节点类型放行——s3 模式外部访问开着，这是唯一能挡住
    `read_text('/etc/passwd')` 的闸门。
    """
    text = _check_where(where)
    if sql_policy() != "strict":  # 显式关掉白名单（config.yaml 的 xlsx.sql_policy）
        return text
    raw = con.execute("SELECT json_serialize_sql(?)", [f"SELECT 1 WHERE {text}"]).fetchone()[0]
    ast = json.loads(raw)
    if ast.get("error"):
        raise ValueError(f"where 无法解析：{ast.get('error_message')}")
    for node, name in _nodes(ast):
        if node not in _ALLOWED_NODES:
            raise ValueError(f"where 不支持 {node.lower()} 表达式")
        if node == "FUNCTION" and name not in _ALLOWED_FUNCTIONS:
            raise ValueError(f"where 不支持函数 {name}()")
    return text


def _nodes(node: Any) -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        kind = node.get("type")
        if kind:
            yield str(kind), str(node.get("function_name") or "")
        for value in node.values():
            yield from _nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _nodes(value)


def _format_value(value: Any) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= _MAX_VALUE_LEN else text[:_MAX_VALUE_LEN] + "…"


def _csv(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_format_value(v) for v in row])
    return buffer.getvalue().rstrip("\n")


def sheet_rows(path: Path) -> int:
    con = _connect()
    try:
        return int(_rows(con, f"SELECT count(*) FROM {_source(path)}")[0][0])
    finally:
        con.close()


def info(sha256: str, sheet: str | None, header: int = 0) -> str:
    manifest = reload_manifest(sha256, header)
    if manifest is None:
        raise FileNotFoundError("派生缓存清单缺失")
    sheets = manifest["sheets"]
    lines = [f"共 {len(sheets)} 个 sheet（每 sheet 一个 Parquet，按内容哈希缓存）："]
    for item in sheets:
        columns = "、".join(item["columns"][:12]) or "（无列）"
        lines.append(f"- {item['name']}：{item['rows']} 行 × {len(item['columns'])} 列")
        lines.append(f"  列：{columns}")
    target = _pick_sheet(sheets, sheet)
    if target is None:
        return "\n".join(lines)
    local = local_parquet(sha256, target["name"], header)
    if local is None:
        return "\n".join(lines)
    con = _connect()
    try:
        preview = _rows(con, f"SELECT * FROM {_source(local)} LIMIT 3")
    finally:
        con.close()
    lines.append("")
    lines.append(f"[预览] sheet={target['name']}")
    lines.append(_csv(target["columns"], preview))
    return "\n".join(lines)


def _pick_sheet(sheets: list[dict[str, Any]], sheet: str | None) -> dict[str, Any] | None:
    if not sheets:
        return None
    if not sheet:
        return sheets[0]
    for item in sheets:
        if item["name"] == sheet:
            return item
    return None


def query(
    sha256: str,
    sheet: str | None,
    where: str | None,
    columns: list[str] | None,
    limit: int,
    offset: int,
    header: int = 0,
) -> str:
    manifest = reload_manifest(sha256, header)
    target = _pick_sheet(manifest["sheets"] if manifest else [], sheet)
    if target is None:
        raise ValueError(f"没有这个 sheet：{sheet}（可选：{[s['name'] for s in (manifest or {}).get('sheets', [])]}）")
    local = local_parquet(sha256, target["name"], header)
    if local is None:
        raise FileNotFoundError(f"派生缓存缺失：{target['name']}")
    selected = [c for c in (columns or []) if c in target["columns"]]
    unknown = [c for c in (columns or []) if c not in target["columns"]]
    projection = ", ".join(_ident(c) for c in selected) if selected else "*"
    size = min(max(int(limit), 1), _MAX_ROWS)
    start = max(int(offset), 0)
    con = _connect()
    try:
        predicate = f" WHERE {_check_predicate(con, where)}" if where else ""
        total = int(_rows(con, f"SELECT count(*) FROM {_source(local)}{predicate}")[0][0])
        rows = _rows(con, f"SELECT {projection} FROM {_source(local)}{predicate} LIMIT {size} OFFSET {start}")
    finally:
        con.close()
    head = f"[{target['name']}] 共 {total} 行匹配，返回第 {start + 1}–{start + len(rows)} 行："
    if unknown:
        head += f"\n（忽略不存在的列：{'、'.join(unknown)}）"
    return head + "\n" + _csv(list(selected) or target["columns"], rows)


def search(sha256: str, keyword: str, sheet: str | None, limit: int, header: int = 0) -> str:
    manifest = reload_manifest(sha256, header)
    sheets = manifest["sheets"] if manifest else []
    targets = [s for s in sheets if sheet is None or s["name"] == sheet]
    if not targets:
        raise ValueError(f"没有这个 sheet：{sheet}")
    needle = f"%{keyword.lower()}%"
    hits: list[str] = []
    cap = min(max(int(limit), 1), _MAX_HITS)
    con = _connect()
    try:
        for item in targets:
            local = local_parquet(sha256, item["name"], header)
            if local is None:
                continue
            columns = item["columns"][:_MAX_COLUMNS]
            if not columns:
                continue
            parts = [
                f"SELECT {_literal(c)} AS col, count(*) AS n FROM {_source(local)} "
                f"WHERE lower(CAST({_ident(c)} AS VARCHAR)) LIKE {_literal(needle)}"
                for c in columns
            ]
            for col, count in _rows(con, " UNION ALL ".join(parts)):
                if count:
                    hits.append(f"sheet={item['name']} 列={col} 命中 {count} 格")
    finally:
        con.close()
    if not hits:
        return f"没有找到「{keyword}」（搜索范围：{'、'.join(t['name'] for t in targets)}）"
    head = f"「{keyword}」命中 {len(hits)} 处（按列聚合）：" if len(hits) > cap else f"「{keyword}」命中："
    return head + "\n" + "\n".join(hits[:cap])


def profile(sha256: str, sheet: str | None, header: int = 0) -> str:
    manifest = reload_manifest(sha256, header)
    target = _pick_sheet(manifest["sheets"] if manifest else [], sheet)
    if target is None:
        raise ValueError(f"没有这个 sheet：{sheet}")
    local = local_parquet(sha256, target["name"], header)
    if local is None:
        raise FileNotFoundError(f"派生缓存缺失：{target['name']}")
    columns = target["columns"][:_MAX_COLUMNS]
    con = _connect()
    try:
        rows = int(_rows(con, f"SELECT count(*) FROM {_source(local)}")[0][0])
        lines = [f"[{target['name']}] {rows} 行 × {len(target['columns'])} 列"]
        for column in columns:
            sql = (
                f'SELECT count(*) FILTER (WHERE "{column}" IS NULL), count(DISTINCT "{column}"), '
                f"min({_ident(column)}), max({_ident(column)}) FROM {_source(local)}"
            )
            nulls, uniq, low, high = _rows(con, sql)[0]
            span = f"{_format_value(low)} ~ {_format_value(high)}" if low is not None else "-"
            lines.append(f"- {column}：空值 {nulls}，唯一值 {uniq}，范围/样例 {span}")
    finally:
        con.close()
    return "\n".join(lines)
