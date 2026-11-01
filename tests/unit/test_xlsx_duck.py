"""派生缓存（ADR-0018 §6）里可以离线测的部分：路径、key、谓词闸门、模式解析。"""

from __future__ import annotations

from pathlib import Path

import pytest

from graphs.tools.xlsx import duck, store

SHA = "a" * 64


def test_store_path_layout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)

    path = duck.store_path(SHA, "数据")

    assert path == tmp_path / "derived" / SHA / "数据.parquet"


@pytest.mark.parametrize("sheet", ["../../etc/passwd", "a/b", "..", "", "x" * 300, 'q"<>|'])
def test_store_path_cannot_escape_hash_dir(tmp_path: Path, monkeypatch, sheet: str) -> None:
    """sheet 名来自上传的表格，不能决定路径（`..`、分隔符、控制字符都压平）。"""
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)

    path = duck.store_path(SHA, sheet)

    assert path.parent == tmp_path / "derived" / SHA
    assert path.name.endswith(".parquet")
    assert len(path.name) <= duck._SHEET_NAME_MAX + len(".parquet")


def test_remote_key_has_prefix_and_hash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(duck, "config", _StubConfig({"xlsx.cache_prefix": "xlsx-derived/"}))

    assert duck.remote_key(SHA, "Sheet1") == f"xlsx-derived/{SHA}/Sheet1.parquet"


class _StubConfig:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str):  # noqa: ANN201
        return self._values[key]


@pytest.mark.parametrize("where", ["1=1; DROP TABLE t", "x = 1 OR (SELECT 1)", "ATTACH 'x' AS y"])
def test_check_where_rejects_multiple_statements(where: str) -> None:
    with pytest.raises(ValueError):
        duck._check_where(where)


@pytest.mark.parametrize("where", ["数量 > 0", "a = 'x' AND b IS NOT NULL", "c BETWEEN 1 AND 9"])
def test_check_where_allows_plain_predicates(where: str) -> None:
    assert duck._check_where(where) == where


def test_cache_mode_falls_back_to_local_without_s3(monkeypatch) -> None:
    monkeypatch.setattr(duck, "config", _StubConfig({"xlsx.cache_mode": "s3"}))
    monkeypatch.setattr(duck.blobs, "configured", lambda: False)

    assert duck.cache_mode() == "local"


def test_cache_mode_off_disables_layer(monkeypatch) -> None:
    monkeypatch.setattr(duck, "config", _StubConfig({"xlsx.cache_mode": "off"}))

    assert duck.cache_enabled() is False


def test_local_parquet_returns_none_when_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(duck, "cache_mode", lambda: "local")

    assert duck.local_parquet(SHA, "Sheet1") is None


def test_local_parquet_returns_existing_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)
    path = tmp_path / "derived" / SHA / "Sheet1.parquet"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"PAR1")

    assert duck.local_parquet(SHA, "Sheet1") == path
