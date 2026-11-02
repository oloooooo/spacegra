"""派生缓存（ADR-0018 §6）里可以离线测的部分：路径、key、谓词闸门、模式解析。"""

from __future__ import annotations

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


def test_remote_key_has_prefix_and_hash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(duck, "config", _StubConfig({"xlsx.cache_prefix": "xlsx-derived/"}))

    assert duck.remote_key(SHA, "Sheet1") == f"xlsx-derived/{SHA}/h0/Sheet1.parquet"
    assert duck.remote_key(SHA, "Sheet1", 1) == f"xlsx-derived/{SHA}/h1/Sheet1.parquet"


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
    with pytest.raises(ValueError):
        duck._check_predicate(parsed, "a IN (SELECT 1)")


def test_local_parquet_reads_s3_directly(monkeypatch) -> None:
    """s3 模式直接给 `s3://` 地址：多主机不需要本地缓存目录（ADR-0018 §6）。"""
    monkeypatch.setattr(duck, "cache_mode", lambda: "s3")
    monkeypatch.setattr(duck, "config", _StubConfig({"xlsx.cache_prefix": "xlsx-derived"}))
    monkeypatch.setattr(duck.blobs, "bucket", lambda: "bench")

    assert duck.local_parquet(SHA, "Sheet1") == f"s3://bench/xlsx-derived/{SHA}/h0/Sheet1.parquet"


def test_ident_escapes_quotes() -> None:
    """列名来自 xlsx 表头，双引号必须翻倍，否则能拼出第二个标识符/谓词。"""
    assert duck._ident('a"b') == '"a""b"'


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


def test_cache_mode_falls_back_to_local_without_s3(monkeypatch) -> None:
    monkeypatch.setattr(duck, "config", _StubConfig({"xlsx.cache_mode": "s3"}))
    monkeypatch.setattr(duck.blobs, "configured", lambda: False)

    assert duck.cache_mode() == "local"


def test_cache_mode_unknown_value_means_local(monkeypatch) -> None:
    """没有 off 这个值：写错/写旧值一律当 local，不会静默关掉缓存。"""
    monkeypatch.setattr(duck, "config", _StubConfig({"xlsx.cache_mode": "off"}))

    assert duck.cache_mode() == "local"


def test_local_parquet_returns_none_when_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(duck, "cache_mode", lambda: "local")

    assert duck.local_parquet(SHA, "Sheet1") is None


def test_local_parquet_returns_existing_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)
    path = tmp_path / "derived" / SHA / "h0" / "Sheet1.parquet"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"PAR1")

    assert duck.local_parquet(SHA, "Sheet1") == path
