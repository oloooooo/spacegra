"""`scripts/reap_upload_orphans.py` 的形状单测：只验纯函数与"删哪些目录"的判定。"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shared import config as app_config


def _reaper():  # noqa: ANN202 —— 模块对象
    path = Path(__file__).resolve().parents[2] / "scripts" / "reap_upload_orphans.py"
    spec = importlib.util.spec_from_file_location("reap_upload_orphans", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclass 需要模块已在 sys.modules 里，否则 __qualname__ 解析不到
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def uploads_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "uploads"
    monkeypatch.setattr(app_config, "get", lambda key: str(root))
    return root


def _write(uploads_dir: Path, owner: str, thread: str, name: str, mtime: datetime, size: int = 1) -> Path:
    path = uploads_dir / owner / thread / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = mtime.timestamp()
    import os

    os.utime(path, (stamp, stamp))
    return path


def test_live_prefix_has_one_shape() -> None:
    """台账前缀与扫描前缀必须是同一种形状，否则 `live` 永远不命中，所有目录都会被删。"""
    reaper = _reaper()
    assert reaper._live_prefixes([("u1", "t1"), ("u2", "t2")]) == {"u1/t1/", "u2/t2/"}


def test_iter_prefixes_matches_live_shape(uploads_dir: Path) -> None:
    reaper = _reaper()
    _write(uploads_dir, "u1", "t1", "note.md", datetime.now(UTC))
    assert set(reaper._iter_prefixes()) == set(reaper._live_prefixes([("u1", "t1")]))


def test_orphans_skip_live_and_fresh(uploads_dir: Path) -> None:
    reaper = _reaper()
    now = datetime.now(UTC)
    _write(uploads_dir, "live", "t", "a.md", now - timedelta(days=3))
    _write(uploads_dir, "fresh", "t", "b.md", now)  # 可能是正在上传
    _write(uploads_dir, "dead", "t", "c.md", now - timedelta(days=3), size=5)

    prefixes, objects, size = reaper._orphans({"live/t/"}, now - timedelta(hours=24))

    assert prefixes == ["dead/t/"]
    assert (objects, size) == (1, 5)


def test_orphans_match_exactly_not_by_prefix(uploads_dir: Path) -> None:
    """`u1/t1/` 不该保护 `u1/t10/`（字符串 startswith 会犯的错）。"""
    reaper = _reaper()
    old = datetime.now(UTC) - timedelta(days=3)
    _write(uploads_dir, "u1", "t1", "a.md", old)
    _write(uploads_dir, "u1", "t10", "b.md", old)

    prefixes, _, _ = reaper._orphans({"u1/t1/"}, datetime.now(UTC) - timedelta(hours=24))

    assert prefixes == ["u1/t10/"]
