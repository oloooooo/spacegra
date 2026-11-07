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
由 `read_xlsx(… range='A<表头行>:…')` 在解析时一次做掉，四个工具不再分叉。

解析交给 DuckDB 的 `excel` 扩展（`read_xlsx`）：openpyxl 只读 `<dimension>` 拿表名
与行列数（不读单元格），用它拼出 `range`。**必须 `header=false`**——`header=true`
时列宽取自表头行，表头单元格为空的那几列会被整列丢掉（连同下面的数据）。表头
由我们自己取第一行来定，空串/空格统一成 NULL。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
import uuid
from collections.abc import Collection, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
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
        "CASE_EXPR",
        "COLLATE",
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
        # `extract(...)` 在这棵语法树里就是 `date_part(...)`
        "date_part",
        "starts_with",
        "ends_with",
        "strpos",
        "regexp_matches",
        "nullif",
        "greatest",
        "least",
    }
)
# 字符串字面量先屏蔽再查文本闸门：`备注 = 'a--b'`、`'select * from x'` 是数据不是 SQL
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'|\$\$.*?\$\$", re.DOTALL)


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


def _connect(extra_dirs: Collection[Path] = ()):  # noqa: ANN202 —— duckdb 在可选依赖里
    """内存库 + 锁配置；`local` 模式再关掉外部访问、只留允许读的目录。

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
    # `LOAD` 必须在设闸门之前：扩展文件在 ``~/.duckdb/extensions/…``，不在
    # allowed_directories 里，闸门一开连自动加载都会被拒。
    try:
        con.execute("LOAD excel")
    except Exception as exc:  # noqa: BLE001 - 装没装是部署问题，给出能照做的下一步
        con.close()
        raise RuntimeError(
            "缺少 DuckDB 的 excel 扩展：先执行 `INSTALL excel`（离线部署要预置 "
            "~/.duckdb/extensions/<duckdb 版本>/<平台>/excel.duckdb_extension）。"
            f"原始错误：{exc}"
        ) from exc
    if cache_mode() == "s3":
        _configure_s3(con)
    else:
        # 顺序不能换：``enable_external_access=false`` 之后 allowed_directories 就改不了了，
        # 而关掉外部访问后它仍是唯一还能读的目录（ADR-0018 §10）。源文件目录也要
        # 显式加进来：`read_xlsx` 由 DuckDB 自己读文件，不是 Python 读的。
        dirs = [_derived_dir(), *extra_dirs]
        allowed = ", ".join(_literal(path.resolve().as_posix()) for path in dirs)
        con.execute(f"SET allowed_directories = [{allowed}]")
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


class _Sheet(NamedTuple):
    name: str
    rows: int  # 数据行数（不含表头）
    columns: int


_CAST_TYPES = ("BIGINT", "DOUBLE", "TIMESTAMP")  # 没有 DATE：`try_cast` 会把带时间的值截掉


def _scan(source: Path, header: int) -> list[_Sheet] | None:
    """只读 `<dimension>` 拿表名与行列数，不碰单元格；超 `xlsx.max_cells` 返回 None。

    `read_xlsx` 的 `range` 必须写全（`A2:` 会被拒），所以范围只能从尺寸推出来。
    缺 `<dimension>` 的工作表 openpyxl 会报 `Worksheet is unsized`，这里翻成
    "请另存为后重传"——猜一个范围只会静默截断。
    """
    from openpyxl import load_workbook

    try:
        book = load_workbook(source, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 坏文件由调用方回给用户
        raise ValueError(f"无法读取工作簿：{exc}") from exc
    try:
        sheets: list[_Sheet] = []
        total = 0
        for ws in book.worksheets:
            try:
                ws.calculate_dimension()  # 缺 dimension 时只有它会明确报错
            except ValueError as exc:
                raise ValueError(f"工作表「{ws.title}」缺少尺寸信息，请用 Excel 另存为后重传") from exc
            rows, columns = int(ws.max_row or 0), int(ws.max_column or 0)
            data_rows = max(rows - int(header) - 1, 0)
            if not data_rows or not columns:
                continue
            total += data_rows * columns
            if total > _MAX_CELLS:
                return None
            sheets.append(_Sheet(str(ws.title), data_rows, columns))
        return sheets
    finally:
        book.close()


def _staging(con: Any, source: Path, sheet: _Sheet, header: int) -> tuple[list[str], list[str]]:
    """把一张表按 `range` 读进临时表 `staging`（带行号，全 VARCHAR）。

    返回 `(staging 里的原始列名, 表头列名)`：前者用于读数据，后者是给用户看的。
    """
    from openpyxl.utils import get_column_letter

    first = int(header) + 1
    span = f"A{first}:{get_column_letter(sheet.columns)}{first + sheet.rows}"
    con.execute("DROP TABLE IF EXISTS staging")
    con.execute(
        f"CREATE TEMP TABLE staging AS SELECT row_number() OVER () AS _rn, * FROM read_xlsx("
        f"{_literal(source.as_posix())}, sheet={_literal(sheet.name)}, "
        f"header=false, all_varchar=true, range={_literal(span)})"
    )
    columns = [row[0] for row in con.execute("DESCRIBE staging").fetchall() if row[0] != "_rn"]
    head = con.execute("SELECT * EXCLUDE (_rn) FROM staging WHERE _rn = 1").fetchone() or ()
    return columns, _clean_columns(list(head[: len(columns)]))


def _castable(con: Any, names: list[str], cast_type: str) -> set[str]:
    """哪些列的非空值全都能转成 `cast_type`（空值不参与判断）。"""
    if not names:
        return set()
    checks = []
    for index, name in enumerate(names):
        quoted = _ident(name)
        filled = f"nullif(trim({quoted}), '')"
        ok = f"try_cast({filled} AS {cast_type}) IS NOT NULL"
        if cast_type == "BIGINT":
            # `try_cast('300.5' AS BIGINT)` 会四舍五入成 301，不报错——所以整数要多一道形状检查。
            ok += f" AND regexp_full_match(trim({quoted}), '^[-+]?[0-9]+$')"
        checks.append(
            f"count(*) FILTER (WHERE {ok}) = count(*) FILTER (WHERE {filled} IS NOT NULL) "
            f"AND count(*) FILTER (WHERE {filled} IS NOT NULL) > 0 AS c{index}"
        )
    row = con.execute(f"SELECT {', '.join(checks)} FROM cleaned").fetchone() or ()
    return {name for name, ok in zip(names, row, strict=False) if ok}


def _projection(names: list[str], types: dict[str, str]) -> str:
    """按证据出的类型拼 SELECT 列表；没有证据的列保持文本。"""
    parts = []
    for name in names:
        value = _ident(name)
        cast_type = types.get(name)
        expression = f"CAST({value} AS {cast_type})" if cast_type else value
        parts.append(f"{expression} AS {value}")
    return ", ".join(parts)


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
        name = ("" if raw is None else str(raw)).strip() or "列"
        count = seen.get(name, 0) + 1
        seen[name] = count
        out.append(name if count == 1 else f"{name}_{count}")
    return out


def build_derived(sha256: str, source: Path, header: int = 0) -> dict[str, Any] | None:
    """把 xlsx 的每个 sheet 转成一份 Parquet（同步，跑在线程池里）。

    返回清单；数据规模超过 `xlsx.max_cells` 时返回 None——那种表任何查询都会打满
    CPU，调用方应当直接回一句"表太大"，而不是先落一份几 GB 的中间产物。
    """
    metas = _scan(source, header)
    if metas is None:
        return None
    con = _connect([source.parent])
    try:
        sheets: list[dict[str, Any]] = []
        for meta in metas:
            columns, names = _staging(con, source, meta, header)
            con.execute(
                "CREATE TEMP TABLE cleaned AS SELECT "
                + ", ".join(
                    f"CASE WHEN trim({_ident(c)}) = '' THEN NULL ELSE {_ident(c)} END AS {_ident(n)}"
                    for c, n in zip(columns, names, strict=True)
                )
                + " FROM staging WHERE _rn > 1"
            )
            con.execute("DROP TABLE staging")
            blank = " AND ".join(f"{_ident(name)} IS NULL" for name in names)
            con.execute(f"DELETE FROM cleaned WHERE {blank}")
            types: dict[str, str] = {}
            pending = list(names)
            for cast_type in _CAST_TYPES:
                ok = _castable(con, pending, cast_type)
                types.update(dict.fromkeys(ok, cast_type))
                pending = [name for name in pending if name not in ok]
            rows = int(con.execute("SELECT count(*) FROM cleaned").fetchone()[0])
            if not rows:
                con.execute("DROP TABLE cleaned")
                continue
            target = store_path(sha256, meta.name, header)
            tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.unlink(missing_ok=True)
                con.execute(
                    f"COPY (SELECT {_projection(names, types)} FROM cleaned) TO "
                    f"{_literal(tmp.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                data = tmp.read_bytes()
            finally:
                con.execute("DROP TABLE IF EXISTS cleaned")
                tmp.unlink(missing_ok=True)
            store.write_atomic(target, data)
            sheets.append(
                {
                    "name": meta.name,
                    "rows": rows,
                    "columns": names,
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
    # s3 模式先把远端写完整、再落本地清单：中途崩掉时本地清单不会指向不存在的远端对象
    if cache_mode() == "s3":
        for sheet in sheets:
            source_file = store_path(sha256, sheet["name"], header)
            blobs.put_bytes(remote_key(sha256, sheet["name"], header), source_file.read_bytes())
        blobs.put_bytes(_remote_manifest_key(sha256, header), json.dumps(manifest, ensure_ascii=False).encode())
    store.write_atomic(_manifest_path(sha256, header), json.dumps(manifest, ensure_ascii=False).encode())
    _enforce_size_limit()
    return manifest


def reload_manifest(sha256: str, header: int = 0) -> dict[str, Any] | None:
    """读清单。本地没有就看对象存储（多实例共享）；远端也没有就返回 None。

    本地清单命中还要确认每个 sheet 的 parquet 都在——缓存预算淘汰是整组删的，
    但旧版本按文件删过，可能留下"清单在、数据没了"的目录。
    """
    path = _manifest_path(sha256, header)
    if path.is_file():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if _parquet_missing(manifest, sha256, header):
            shutil.rmtree(path.parent, ignore_errors=True)
            return None
        return manifest
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
            # 本地清单可能还在（清单本地优先命中）：一起清掉，否则查询会一直指向已删的远端对象
            tail = key[len(prefix) :].split("/")
            if len(tail) >= 2 and len(tail[0]) == 64:
                shutil.rmtree(_derived_dir() / tail[0], ignore_errors=True)
    return len(victims), total


def _parquet_missing(manifest: dict[str, Any], sha256: str, header: int) -> bool:
    return any(not store_path(sha256, s["name"], header).is_file() for s in manifest.get("sheets") or [])


def _enforce_size_limit() -> None:
    """本机缓存超过预算就按"最久未改动"整组淘汰（`<sha>/h<n>/` 连清单一起删）。

    只删 parquet 会留下"清单命中、数据缺失"的目录，查询会一直报缺失——组内所有
    文件同生共死，删掉整组等于下次查询重新解析一次。
    """
    limit_mb = int(config.get("xlsx.cache_local_max_mb"))
    if limit_mb <= 0:
        return
    root = _derived_dir()
    if not root.is_dir():
        return
    groups: list[tuple[float, int, Path]] = []
    for group in root.glob("*/*"):
        if not group.is_dir():
            continue
        files = [p for p in group.rglob("*") if p.is_file()]
        if files:
            stats = [(p.stat().st_mtime, p.stat().st_size) for p in files]
            groups.append((max(m for m, _ in stats), sum(s for _, s in stats), group))
    total = sum(size for _, size, _ in groups)
    limit = limit_mb * 1024 * 1024
    for _, size, group in sorted(groups):
        if total <= limit:
            break
        shutil.rmtree(group, ignore_errors=True)
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
    字符串字面量先屏蔽再查——`备注 LIKE '%--%'` 里的 `--` 是数据，不该被当成注释（#17）。
    """
    text = where.strip().rstrip(";").strip()
    bare = _STRING_LITERAL.sub(lambda m: "_" * len(m.group()), text)
    if ";" in bare or "--" in bare or "/*" in bare or _FORBIDDEN_SQL.search(bare):
        raise ValueError("where 只能是筛选谓词，不能包含分号、注释或 DDL/多条语句")
    return text


def _check_predicate(con: Any, where: str) -> str:
    """文本闸门 + 语法树白名单：不许子查询、表函数和白名单外的函数。

    文本黑名单挡不住 ``EXISTS (FROM query('FROM read_text(…'))`` 这种拼法；这里让
    DuckDB 自己解析成语法树，按节点类型放行——s3 模式外部访问开着，这是唯一能挡住
    `read_text('/etc/passwd')` 的闸门。
    """
    text = _check_where(where)
    if sql_policy() == "free":  # 显式关掉白名单（config.yaml 的 xlsx.sql_policy）；未知值按 strict 处理
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
                raise FileNotFoundError(f"派生缓存缺失：{item['name']}（请删除后重新上传）")
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
                f"SELECT count(*) FILTER (WHERE {_ident(column)} IS NULL), count(DISTINCT {_ident(column)}), "
                f"min({_ident(column)}), max({_ident(column)}) FROM {_source(local)}"
            )
            nulls, uniq, low, high = _rows(con, sql)[0]
            span = f"{_format_value(low)} ~ {_format_value(high)}" if low is not None else "-"
            lines.append(f"- {column}：空值 {nulls}，唯一值 {uniq}，范围/样例 {span}")
    finally:
        con.close()
    return "\n".join(lines)
