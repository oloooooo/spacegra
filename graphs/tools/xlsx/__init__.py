"""xlsx 域：info / query / search / profile 四个只读工具 + 共享 schema。

- 路径以 ``/`` 开头当虚拟路径：``/空间名/x.xlsx`` 是知识空间里的存量文件（本地盘）。
  读不到时直接落到本地文件，再走同一套 SQLite 索引，所以四个工具的实现只有一份。
- pandas 是同步库，tool 统一 ``async def`` + ``asyncio.to_thread`` 跑同步实现，不卡事件循环；
- 每个 tool 注入 ``runtime: ToolRuntime``，``_run`` 统一用 ``stream_writer`` 打
  start/done/error 关键消息（调用方要开 ``custom`` 流模式才收得到）；
- xlsx 是 zip 二进制，read/grep 读不了，查表一律走这四个工具。

agent 视角::

    xlsx_info(path)                                   # 有哪些 sheet、表头、形状、前几行预览
    xlsx_query(path, where="数量 > 0", columns=[...])  # 按列条件筛行（where 是 SQL 谓词）
    xlsx_search(path, keyword="ABC")                  # 全表撒网找关键词，返回 sheet/行/列
    xlsx_profile(path)                                # 空值/重复/类型/数值范围画像

索引为什么必要：80k×10×3 的表现在一次全读 41.7 s，建好索引后 `WHERE … LIMIT 200` 是 2 ms。

两条读路径（ADR-0018 §8）：格子数 ≥ `xlsx.cache_min_cells` 的表第一次被查时按 sheet
转成 Parquet（`duck.py`，介质由 `xlsx.cache_mode` 定 local/s3），之后走 DuckDB `read_parquet`；
小表与 `cache_mode: off` 走本文的 SQLite 索引。对外四个工具的行为一致。
"""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime, tool

from graphs.access.spaces import allowed_spaces
from graphs.tools.xlsx import duck, index, store
from graphs.tools.xlsx.schema import (
    XlsxHeader,
    XlsxInfoArgs,
    XlsxLimit,
    XlsxOffset,
    XlsxPath,
    XlsxProfileArgs,
    XlsxQueryArgs,
    XlsxSearchArgs,
    XlsxSheet,
)
from shared import config as app_config

# 兼容老调用点（测试里 monkeypatch 过这个模块级名字）
_root = store.root


def _check_scope(path: str | Path, config: object) -> None:
    """空间隔离（ADR-0005）：XLSX_TOOLS 不经 backend（自己读字节），必须在 tool 层拦。

    三种放行：``/<空间名>/…`` 的首段 ∈ allowed spaces；根散文件共享只读；Mirage 模式下
    ``/uploads/…``（本会话的上传文件，上传那一刻 API 已经按会话鉴过权）。

    判定落在**解析后**的路径上：`/空间A/../空间B/x.xlsx` 首段是 A（有权限），但真正
    被打开的是 B 的文件。只比对原始字符串等于不设防（带 `..` 与符号链接一起兜住）。
    """
    spaces = allowed_spaces(config)  # type: ignore[arg-type]
    if spaces is None:
        return
    raw = str(path).strip()
    if not raw.startswith(("/", "\\")):
        raise ValueError(f"受限模式下必须用以 / 开头的虚拟路径（/空间名/文件.xlsx）：{path}")
    root = _root()
    try:
        # resolve 不要求路径存在，所以不会把「文件不存在」提前偷成别的错
        target = (root / raw.lstrip("/\\")).resolve()
    except OSError as e:  # 非法字符 / 保留名 / 路径过长
        raise ValueError(f"路径非法：{path}") from e
    if target != root and root not in target.parents:
        raise ValueError(f"路径越界：{path}")
    parts = target.relative_to(root).parts
    if not parts:
        return  # "/" 本身；ls 类调用方自己按规则过滤
    first = parts[0]
    if first == "uploads" and _mirage_mode():
        # 见上加粗两行：上传文件由 API 侧按会话鉴权，profile 又只挂本会话那一个目录，
        # 这里不按空间名判。判定落在**解析后**的首段上，`/uploads/../x/…` 仍会被下面拦住。
        return
    if first in spaces:
        return  # 文件是否存在由 store.resolve_local 报
    if len(parts) == 1 and target.is_file():
        return  # 根散文件共享只读（Q3b）
    visible = sorted(spaces) or ["（无）"]
    raise ValueError(f"无权访问 {path}：{first} 不在你的空间列表 {visible} 内")


def _mirage_mode() -> bool:
    """读文件走 Mirage（对象存储）还是本地磁盘（`okf.backend`）。见 ADR-0018。"""
    return str(app_config.get("okf.backend")).strip().lower() == "s3fs"


async def _local_copy(path: str, runtime: ToolRuntime) -> str:
    """把虚拟路径下的 xlsx 落到本地缓存，返回本地路径。

    本地磁盘模式原样返回（工具本来就是直接读盘的）；s3fs 模式对象存储里没有本地路径，
    必须先取字节：pandas/openpyxl 读不了 HTTP，索引也要一个真实的文件。
    """
    if not _mirage_mode():
        return path
    from graphs.access.mirage_backend import download_bytes

    data = await download_bytes(runtime.config, path)
    if data is None:
        raise FileNotFoundError(f"对象存储里没有 {path}")
    return str(store.virtual_cache_path(path, data))


def _conn(path: str, header: int) -> sqlite3.Connection:
    """本地 xlsx → 索引 → 只读连接（同步，跑在线程池里）。"""
    xlsx, cache_key = store.materialize(path)
    return index.open_ro(index.ensure(xlsx, store.cache_dir(), cache_key, header))


async def _derived(local: str, header: int) -> str | None:
    """小表不值得转 Parquet（解析本身就要一秒级），直接走老索引。

    行数阈值用 openpyxl read_only 探（`index.cell_count`，实测 27 ms），不读整表。
    """
    if header:
        return None
    if duck.cache_mode() == "off":
        return None
    cells = await asyncio.to_thread(index.cell_count, Path(local))
    if cells < int(app_config.get("xlsx.cache_min_cells")):
        return None
    return await duck.ensure(Path(local))


# ---------- 同步实现（跑在线程池里） ----------


def _info(local: str, display: str, sha256: str | None, sheet: str | None, header: int) -> str:
    if sha256:
        return duck.info(sha256, sheet)
    with closing(_conn(local, header)) as conn:
        return index.info(conn, display, sheet)


def _query(
    local: str,
    _display: str,
    sha256: str | None,
    sheet: str | None,
    header: int,
    columns: list[str] | None,
    where: str | None,
    limit: int,
    offset: int,
) -> str:
    if sha256:
        return duck.query(sha256, sheet, where, columns, limit, offset)
    with closing(_conn(local, header)) as conn:
        return index.query(conn, sheet, columns, where, limit, offset)


def _search(
    local: str,
    _display: str,
    sha256: str | None,
    keyword: str,
    sheet: str | None,
    header: int,
    limit: int,
) -> str:
    if sha256:
        return duck.search(sha256, keyword, sheet, limit)
    with closing(_conn(local, header)) as conn:
        return index.search(conn, keyword, sheet, header, limit)


def _profile(local: str, _display: str, sha256: str | None, sheet: str | None, header: int) -> str:
    if sha256:
        return duck.profile(sha256, sheet)
    with closing(_conn(local, header)) as conn:
        return index.profile(conn, sheet)


# ---------- 异步 tool（agent 用这一层） ----------


async def _run(name: str, fn: Any, runtime: ToolRuntime, /, path: str, header: int, *args: Any) -> str:
    """同步实现丢线程池；异常转成错误文本返回，让 agent 能自我纠正而不是炸图。"""
    writer = runtime.stream_writer
    writer({"tool": name, "event": "start", "args": [path, *args]})
    try:
        _check_scope(path, runtime.config)
        local = await _local_copy(path, runtime)
        derived = await _derived(local, header)
        result = await asyncio.to_thread(fn, local, str(path), derived, *args)
    except Exception as e:  # noqa: BLE001 —— 故意兜底，错误信息是给 agent 看的
        writer({"tool": name, "event": "error", "error": f"{type(e).__name__}: {e}"})
        return f"错误：{type(e).__name__}: {e}"
    writer({"tool": name, "event": "done"})
    return result


@tool(args_schema=XlsxInfoArgs)
async def xlsx_info(path: XlsxPath, sheet: XlsxSheet = None, header: XlsxHeader = 0, *, runtime: ToolRuntime) -> str:
    """查看 xlsx 概况：有哪些 sheet、每个 sheet 的行数和列名、前 3 行预览。

    Args:
        path: xlsx 虚拟路径，如 /空间名/文件.xlsx
        sheet: 要预览的 sheet 名，缺省预览第一个
        header: 表头所在行号（0 起）。表格上方有标题行时传 1
    """
    return await _run("xlsx_info", _info, runtime, path, header, sheet)


@tool(args_schema=XlsxQueryArgs)
async def xlsx_query(
    path: XlsxPath,
    where: str | None = None,
    columns: list[str] | None = None,
    sheet: XlsxSheet = None,
    header: XlsxHeader = 0,
    limit: XlsxLimit = 20,
    offset: XlsxOffset = 0,
    *,
    runtime: ToolRuntime,
) -> str:
    """按条件查 xlsx 的行，返回 CSV 文本。适合"已知列名"的查询。

    `where` 是 **SQL 谓词**（不是 pandas 表达式），三条差异必须注意：
    1. 列名含空格/括号/中文以外的符号时用双引号包起来：`"价 格" > 0`；
    2. 空值用 `IS NULL` / `IS NOT NULL`，**`!=` 会漏掉空值行**（SQL 里 NULL 参与比较不成立）；
    3. 不支持 `@变量`、`.str.xxx`、`~` 取反这类 pandas 写法。
    其余（`> < >= <= = != and or in between like`）照常。

    Args:
        path: xlsx 虚拟路径，如 /空间名/文件.xlsx
        where: SQL 谓词，如 "数量 > 0"、"料号 = 'ABC123'"、"a > 1 AND b = 'x'"；不传则全表
        columns: 只返回这些列，缺省返回全部列（宽表建议指定）
        sheet: sheet 名，缺省第一个
        header: 表头所在行号（0 起）
        limit: 最多返回行数（单次上限 200）
        offset: 跳过前 N 行（翻页用）
    """
    return await _run("xlsx_query", _query, runtime, path, header, sheet, columns, where, limit, offset)


@tool(args_schema=XlsxSearchArgs)
async def xlsx_search(
    path: XlsxPath,
    keyword: str,
    sheet: XlsxSheet = None,
    header: XlsxHeader = 0,
    limit: XlsxLimit = 50,
    *,
    runtime: ToolRuntime,
) -> str:
    """全表搜索关键词（字面匹配，不区分大小写），返回命中的 sheet/行/列/值。

    适合"不知道关键词在哪列"的排查；某一列本来就反复出现的值（如 site=1）不要用它，
    改用 `xlsx_query` 按列筛。命中按**单元格**计，一行两列都命中会出两条。

    Args:
        path: xlsx 虚拟路径，如 /空间名/文件.xlsx
        keyword: 要找的文本（字面匹配，不是正则，`%`/`_` 按普通字符处理）
        sheet: 只搜这个 sheet，缺省搜全部 sheet
        header: 表头所在行号（0 起）
        limit: 最多返回命中数（单次上限 100）
    """
    return await _run("xlsx_search", _search, runtime, path, header, keyword, sheet, limit)


@tool(args_schema=XlsxProfileArgs)
async def xlsx_profile(path: XlsxPath, sheet: XlsxSheet = None, header: XlsxHeader = 0, *, runtime: ToolRuntime) -> str:
    """数据质量画像：每列的空值数、唯一值数、数值范围（或文本样例），以及重复行数。

    Args:
        path: xlsx 虚拟路径，如 /空间名/文件.xlsx
        sheet: sheet 名，缺省第一个
        header: 表头所在行号（0 起）
    """
    return await _run("xlsx_profile", _profile, runtime, path, header, sheet)


def _expose_model_schema(t: Any) -> Any:
    """让 args_schema 用模型可见的那份（tool_call_schema）。

    langchain-quickjs 用 args_schema 渲染 JS 里的 tools.x 签名，而带 ``runtime: ToolRuntime``
    的 args_schema 生成不出 JSON Schema（CallableSchema），会退化成
    ``(input: Record<string, unknown>)``，模型就瞎传 ``{input: "xx.xlsx"}`` 而报
    "path: Field required"。换掉后签名正常，runtime 仍按函数签名注入。
    """
    t.args_schema = t.tool_call_schema
    return t


XLSX_TOOLS = [_expose_model_schema(t) for t in (xlsx_info, xlsx_query, xlsx_search, xlsx_profile)]
