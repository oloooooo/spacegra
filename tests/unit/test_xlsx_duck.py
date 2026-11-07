"""派生缓存（ADR-0018 §6）里可以离线测的部分：路径、key、谓词闸门、模式解析。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphs.tools.xlsx import duck, store

SHA = "a" * 64


def test_store_path_layout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)

    path = duck.store_path(SHA, "数据")

    assert path == tmp_path / "derived" / SHA / "h0" / "数据.parquet"


def test_store_path_separates_headers(tmp_path: Path, monkeypatch) -> None:
    """表头行号进缓存键：同一份文件用不同 header 查，不能共用一份 parquet。"""
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)

    assert duck.store_path(SHA, "数据", 2) != duck.store_path(SHA, "数据", 0)
    assert duck.store_path(SHA, "数据", 2).parent.name == "h2"


@pytest.mark.parametrize("sheet", ["../../etc/passwd", "a/b", "..", "", "x" * 300, 'q"<>|'])
def test_store_path_cannot_escape_hash_dir(tmp_path: Path, monkeypatch, sheet: str) -> None:
    """sheet 名来自上传的表格，不能决定路径（`..`、分隔符、控制字符都压平）。"""
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)

    path = duck.store_path(SHA, sheet)

    assert path.parent == tmp_path / "derived" / SHA / "h0"
    assert path.name.endswith(".parquet")
    assert len(path.name) <= duck._SHEET_NAME_MAX + len(".parquet")


class _StubConfig:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str):  # noqa: ANN201
        return self._values[key]


@pytest.mark.parametrize(
    "where",
    ["1=1; DROP TABLE t", "x = 1 OR (SELECT 1)", "ATTACH 'x' AS y", "1=1 --", "1=1 /* x */"],
)
def test_check_where_rejects_multiple_statements(where: str) -> None:
    with pytest.raises(ValueError):
        duck._check_where(where)


@pytest.mark.parametrize("where", ["数量 > 0", "a = 'x' AND b IS NOT NULL", "c BETWEEN 1 AND 9"])
def test_check_where_allows_plain_predicates(where: str) -> None:
    assert duck._check_where(where) == where


@pytest.fixture
def parsed(tmp_path: Path, monkeypatch):
    """谓词校验要真解析，所以建一次连接（local 模式，不碰网络）。"""
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)
    con = duck._connect()
    try:
        yield con
    finally:
        con.close()


@pytest.mark.parametrize(
    "where",
    [
        "a > 1 AND b LIKE '%x%'",
        "数量 BETWEEN 1 AND 9",
        "c || 'x' = 'y'",
        "CAST(a AS VARCHAR) = '1'",
        "NOT a = 1",
        "strftime(a, '%Y') = '2024'",
        "coalesce(a, 0) > 1",
        # #17：常见的分析谓词不该被白名单误伤
        "CASE WHEN a > 1 THEN '大' ELSE '小' END = '大'",
        "extract(year from 日期) = 2024",
        "starts_with(客户, '甲')",
        "ends_with(客户, '公司')",
        "nullif(金额, 0) > 1",
        "greatest(a, b) > 1",
        "strpos(客户, '甲') > 0",
        "regexp_matches(客户, '^甲')",
        # 字面量里的 -- / ; / 关键字是数据，不是注释或语句（#17）
        "备注 = 'a--b'",
        "备注 = 'select * from x'",
        "备注 = 'x;y'",
    ],
)
def test_check_predicate_allows_plain_predicates(parsed, where: str) -> None:
    """合法谓词照旧放行——闸门不能把模型的正常写法也堵死。"""
    assert duck._check_predicate(parsed, where) == where


@pytest.mark.parametrize(
    "where",
    [
        "EXISTS (FROM query('FROM read_text(''/etc/passwd'')'))",
        "read_text('/etc/passwd') IS NOT NULL",
        "glob('*') IS NOT NULL",
        "a IN (SELECT 1)",
    ],
)
def test_check_predicate_rejects_subqueries_and_table_functions(parsed, where: str) -> None:
    """s3 模式外部访问开着，语法树闸门是挡住 `read_text` / `query(...)` 的唯一一层。"""
    with pytest.raises(ValueError):
        duck._check_predicate(parsed, where)


def test_sql_policy_defaults_to_strict(monkeypatch) -> None:
    monkeypatch.delenv("XLSX_SQL_POLICY", raising=False)
    assert duck.sql_policy() == "strict"


def test_free_policy_only_keeps_the_text_gate(parsed, monkeypatch) -> None:
    """`free` 是显式退路：子查询/拼接载荷放行，多条语句与注释照样拦。"""
    monkeypatch.setenv("XLSX_SQL_POLICY", "free")
    bypass = "EXISTS (FROM query('FROM read_' || 'text(''/etc/passwd'')'))"
    assert duck._check_predicate(parsed, bypass) == bypass
    with pytest.raises(ValueError):
        duck._check_predicate(parsed, "a = 1; DROP TABLE t")


def test_unknown_policy_falls_back_to_strict(parsed, monkeypatch) -> None:
    monkeypatch.setenv("XLSX_SQL_POLICY", "loose")
    # 能穿过文本闸门、只有语法树会拦的载荷，才看得出策略是真 strict 还是 fail-open
    with pytest.raises(ValueError, match="subquery"):
        duck._check_predicate(parsed, "EXISTS (FROM query('FROM read_' || 'text(''/etc/passwd'')'))")


def test_ident_escapes_quotes() -> None:
    """列名来自 xlsx 表头，双引号必须翻倍，否则能拼出第二个标识符/谓词。"""
    assert duck._ident('a"b') == '"a""b"'


def test_profile_escapes_hostile_column_names(tmp_path: Path, monkeypatch) -> None:
    """表头里带 `"` 的列不能改写 profile 的 SQL（旧实现只在 min/max 上转了义）。"""
    import duckdb  # noqa: PLC0415 —— 可选依赖

    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)
    hostile = "x\"), count(*), min(x), max(x) FROM read_parquet('/etc/passwd') --"
    target = duck.store_path(SHA, "Sheet1")
    target.parent.mkdir(parents=True)
    writer = duckdb.connect(":memory:")
    writer.execute(f"COPY (SELECT 1 AS {duck._ident(hostile)}) TO {duck._literal(target.as_posix())} (FORMAT PARQUET)")
    writer.close()
    duck._manifest_path(SHA).write_text(
        json.dumps(
            {
                "version": 2,
                "sha256": SHA,
                "header": 0,
                "engine": "duckdb",
                "sheets": [{"name": "Sheet1", "rows": 1, "columns": [hostile], "bytes": 1}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    text = duck.profile(SHA, "Sheet1")

    assert "1 行 × 1 列" in text


def test_connect_reads_only_derived_dir(tmp_path: Path, monkeypatch) -> None:
    """issue #3：只限 allowed_directories 不够，外部访问必须整体关掉。

    `read_text`/`glob` 不受 allowed_directories 约束，只有 `enable_external_access=false` 能拦。
    """
    import duckdb  # noqa: PLC0415 —— 可选依赖，只在装了它的环境跑

    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)
    allowed = tmp_path / "derived" / "ab" / "S.parquet"
    allowed.parent.mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    writer = duckdb.connect(":memory:")
    writer.execute(f"COPY (SELECT 1 AS x) TO {duck._literal(allowed.as_posix())} (FORMAT PARQUET)")
    writer.close()

    con = duck._connect()
    try:
        assert con.execute(f"SELECT count(*) FROM {duck._source(allowed)}").fetchone() == (1,)
        for sql in (
            f"SELECT content FROM read_text({duck._literal((tmp_path / 'secret.txt').as_posix())})",
            f"SELECT * FROM glob({duck._literal(tmp_path.as_posix() + '/*')})",
        ):
            with pytest.raises(Exception, match="Cannot access file"):
                con.execute(sql)
    finally:
        con.close()


def test_local_parquet_returns_existing_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)
    path = tmp_path / "derived" / SHA / "h0" / "Sheet1.parquet"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"PAR1")

    assert duck.local_parquet(SHA, "Sheet1") == path


def test_scan_refuses_oversized_workbook_before_parsing(tmp_path: Path, monkeypatch) -> None:
    """先看规模再解析：只读 `<dimension>`，超 `xlsx.max_cells` 直接拒（#11）。"""
    import pandas as pd

    source = tmp_path / "big.xlsx"
    pd.DataFrame({"a": [1, 2], "b": [3, 4]}).to_excel(source, index=False)
    monkeypatch.setattr(duck, "_MAX_CELLS", 1)

    assert duck._scan(source, 0) is None


def test_scan_reports_sheet_shapes(tmp_path: Path) -> None:
    """表头行不算数据行；没有数据行的 sheet 不出现。"""
    import pandas as pd

    source = tmp_path / "small.xlsx"
    with pd.ExcelWriter(source) as writer:
        pd.DataFrame({"a": [1, 2, 3]}).to_excel(writer, sheet_name="数据", index=False)
        pd.DataFrame().to_excel(writer, sheet_name="空表", index=False)

    assert duck._scan(source, 0) == [duck._Sheet(name="数据", rows=3, columns=1)]


def test_build_derived_keeps_columns_with_empty_header(tmp_path: Path, monkeypatch) -> None:
    """表头单元格为空不能把整列丢掉（`header=true` 会丢），行数也不含表头。"""
    import duckdb
    from openpyxl import Workbook

    try:
        duckdb.connect().execute("LOAD excel")
    except Exception:  # noqa: BLE001 - 没装 excel 扩展的环境跳过
        pytest.skip("duckdb excel 扩展未安装")
    source = tmp_path / "src.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "数据"
    sheet.append(["客户", "金额", None])
    sheet.append(["甲", 100, "备注值"])
    book.save(source)
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path / "cache")

    manifest = duck.build_derived("deadbeef", source, 0)

    assert manifest is not None
    assert manifest["sheets"] == [
        {
            "name": "数据",
            "rows": 1,
            "columns": ["客户", "金额", "列"],
            "bytes": manifest["sheets"][0]["bytes"],
        }
    ]


def test_size_limit_drops_whole_cache_group(tmp_path: Path, monkeypatch) -> None:
    """清单和 parquet 必须一起删：只删 parquet 会留下"清单在、数据没了"的目录（#10）。"""
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(duck, "config", _StubConfig({"xlsx.cache_local_max_mb": 1}))
    group = tmp_path / "derived" / SHA / "h0"
    group.mkdir(parents=True)
    (group / "manifest.json").write_bytes(b"{}")
    (group / "Sheet1.parquet").write_bytes(b"x" * (700 * 1024))
    (group / "Sheet2.parquet").write_bytes(b"x" * (700 * 1024))

    duck._enforce_size_limit()

    assert not group.exists()
