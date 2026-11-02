"""上传介质层（`shared.session.uploads`）的单测：不碰真对象存储、不碰真磁盘之外的东西。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from shared import config as app_config
from shared.session import uploads


@pytest.fixture
def local_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 uploads.dir 指到临时目录，并把介质固定成 fs。"""

    def fake_get(key: str):
        return {"uploads.dir": str(tmp_path / "uploads"), "uploads.store": "fs"}[key]

    monkeypatch.setattr(app_config, "get", fake_get)
    return tmp_path / "uploads"


class TestStoreMode:
    def test_auto_without_object_storage_is_fs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app_config, "get", lambda key: "auto")
        monkeypatch.setattr(uploads.blobs, "configured", lambda: False)
        assert uploads.store_mode() == "fs"

    def test_auto_with_object_storage_is_s3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app_config, "get", lambda key: "auto")
        monkeypatch.setattr(uploads.blobs, "configured", lambda: True)
        assert uploads.store_mode() == "s3"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("fs", "fs"),
            ("FS", "fs"),
            (" s3 ", "s3"),
            ("garbage", "s3"),  # 未知值 = auto，这里配了对象存储所以是 s3
            ("", "s3"),
            (None, "s3"),
        ],
    )
    def test_explicit_values_win(self, monkeypatch: pytest.MonkeyPatch, value: object, expected: str) -> None:
        monkeypatch.setattr(app_config, "get", lambda key: value)
        monkeypatch.setattr(uploads.blobs, "configured", lambda: True)
        assert uploads.store_mode() == expected


class TestFsPut:
    def test_round_trip(self, local_dir: Path) -> None:
        key, sha256, size = uploads.put_stream(
            io.BytesIO(b"hello"), owner="u1", thread_id="t1", name="a.md", max_bytes=1024
        )
        assert key == "u1/t1/a.md"
        assert size == 5
        assert sha256 == __import__("hashlib").sha256(b"hello").hexdigest()
        assert uploads.local_path("u1", "t1", "a.md").read_bytes() == b"hello"

    def test_overwrite_leaves_no_tmp(self, local_dir: Path) -> None:
        for body in (b"one", b"two-longer"):
            uploads.put_stream(io.BytesIO(body), owner="u1", thread_id="t1", name="x.bin", max_bytes=1024)
        target = uploads.local_path("u1", "t1", "x.bin")
        assert target.read_bytes() == b"two-longer"
        assert [p.name for p in target.parent.iterdir()] == ["x.bin"]

    def test_too_large_cleans_up(self, local_dir: Path) -> None:
        with pytest.raises(uploads.UploadTooLarge):
            uploads.put_stream(io.BytesIO(b"x" * 64), owner="u1", thread_id="t1", name="big.bin", max_bytes=8)
        assert not uploads.local_path("u1", "t1", "big.bin").exists()
        assert list((local_dir / "u1" / "t1").iterdir()) == []

    def test_keys_are_thread_scoped(self, local_dir: Path) -> None:
        uploads.put_stream(io.BytesIO(b"a"), owner="u1", thread_id="t1", name="same.md", max_bytes=64)
        uploads.put_stream(io.BytesIO(b"b"), owner="u1", thread_id="t2", name="same.md", max_bytes=64)
        assert uploads.read_bytes("u1", "t1", "same.md") == b"a"
        assert uploads.read_bytes("u1", "t2", "same.md") == b"b"


class TestMaterialize:
    def test_fs_mode_returns_existing_file(self, local_dir: Path) -> None:
        uploads.put_stream(io.BytesIO(b"x"), owner="u1", thread_id="t1", name="a.md", max_bytes=64)
        assert uploads.materialize("u1", "t1", "a.md") == uploads.local_path("u1", "t1", "a.md")
        assert uploads.materialize("u1", "t1", "missing.md") is None

    def test_s3_mode_always_repulls(self, local_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """多实例下本地副本可能是别的机器留下的旧字节，所以每次都重新 GET（#6）。"""
        monkeypatch.setattr(app_config, "get", lambda key: {"uploads.dir": str(local_dir), "uploads.store": "s3"}[key])
        monkeypatch.setattr(uploads.blobs, "upload_key", lambda o, t, n: f"uploads/{o}/{t}/{n}")
        calls: list[str] = []
        bodies = [b"v1", b"v2"]

        def fake_get_or_none(key: str):
            calls.append(key)
            return bodies[len(calls) - 1]

        monkeypatch.setattr(uploads.blobs, "get_or_none", fake_get_or_none)
        first = uploads.materialize("u1", "t1", "a.xlsx")
        assert first is not None and first.read_bytes() == b"v1"
        second = uploads.materialize("u1", "t1", "a.xlsx")
        assert second == first
        assert second.read_bytes() == b"v2", "必须读到对象存储里的新字节"
        assert len(calls) == 2

    def test_s3_mode_missing_object(self, local_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app_config, "get", lambda key: {"uploads.dir": str(local_dir), "uploads.store": "s3"}[key])
        monkeypatch.setattr(uploads.blobs, "upload_key", lambda o, t, n: "uploads/x")
        monkeypatch.setattr(uploads.blobs, "get_or_none", lambda key: None)
        assert uploads.materialize("u1", "t1", "nope.xlsx") is None


class TestPathContainment:
    """owner/thread/name 都可能来自客户端，任何一段越界都不收（issue #1）。"""

    @pytest.mark.parametrize(("owner", "thread"), [("..", "t1"), ("u1", ".."), ("../x", "t1"), ("u1", "t1/../..")])
    def test_rejects_owner_or_thread(self, local_dir: Path, owner: str, thread: str) -> None:
        with pytest.raises(ValueError):
            uploads.local_dir(owner, thread)
        with pytest.raises(ValueError):
            uploads.local_path(owner, thread, "a.md")

    @pytest.mark.parametrize("name", ["../../evil.pth", "a/b.md", "a\\b.md", "x.pth:ads", "trailing."])
    def test_rejects_name(self, local_dir: Path, name: str) -> None:
        with pytest.raises(ValueError):
            uploads.local_path("u1", "t1", name)

    def test_keeps_chinese_names_under_root(self, local_dir: Path) -> None:
        path = uploads.local_path("张三-9d6966", "t1", "碳排放 1.xlsx")
        assert path == (local_dir / "张三-9d6966" / "t1" / "碳排放 1.xlsx").resolve()
        assert path.is_relative_to(local_dir.resolve())

    def test_put_stream_refuses_dotdot(self, local_dir: Path) -> None:
        with pytest.raises(ValueError):
            uploads.put_stream(io.BytesIO(b"x"), owner="..", thread_id="..", name="evil.pth", max_bytes=64)
        assert list(local_dir.parent.glob("evil*")) == []


class TestCleanup:
    def test_delete_removes_local_file(self, local_dir: Path) -> None:
        uploads.put_stream(io.BytesIO(b"x"), owner="u1", thread_id="t1", name="a.md", max_bytes=64)
        uploads.delete("u1", "t1", "a.md")
        assert not uploads.local_path("u1", "t1", "a.md").exists()

    def test_delete_thread_removes_dir(self, local_dir: Path) -> None:
        uploads.put_stream(io.BytesIO(b"x"), owner="u1", thread_id="t1", name="a.md", max_bytes=64)
        uploads.delete_thread("u1", "t1")
        assert not uploads.local_dir("u1", "t1").exists()

    def test_iter_local_keys_skips_tmp(self, local_dir: Path) -> None:
        uploads.put_stream(io.BytesIO(b"x"), owner="u1", thread_id="t1", name="a.md", max_bytes=64)
        (uploads.local_dir("u1", "t1") / ".tmp-halfwritten").write_bytes(b"..")
        assert [(o, t, n) for o, t, n, _ in uploads.iter_local_keys()] == [("u1", "t1", "a.md")]


class TestBackup:
    """覆盖上传先挪走旧字节（issue #7）：台账/提交失败要能换回来。"""

    def test_restore_brings_old_bytes_back(self, local_dir: Path) -> None:
        uploads.put_stream(io.BytesIO(b"old"), owner="u1", thread_id="t1", name="a.md", max_bytes=64)
        token = uploads.backup("u1", "t1", "a.md")
        assert token is not None
        assert not uploads.local_path("u1", "t1", "a.md").exists(), "备份之后原名不在"
        assert uploads.local_path("u1", "t1", token).read_bytes() == b"old"
        uploads.put_stream(io.BytesIO(b"new"), owner="u1", thread_id="t1", name="a.md", max_bytes=64)
        assert uploads.read_bytes("u1", "t1", "a.md") == b"new"

        uploads.restore("u1", "t1", "a.md", token)
        assert uploads.read_bytes("u1", "t1", "a.md") == b"old"

    def test_backup_without_old_file_is_none(self, local_dir: Path) -> None:
        assert uploads.backup("u1", "t1", "missing.md") is None

    def test_drop_backup_leaves_target_alone(self, local_dir: Path) -> None:
        uploads.put_stream(io.BytesIO(b"old"), owner="u1", thread_id="t1", name="a.md", max_bytes=64)
        token = uploads.backup("u1", "t1", "a.md")
        assert token is not None
        uploads.drop_backup("u1", "t1", token)
        assert not uploads.local_path("u1", "t1", token).exists()
        assert uploads.materialize("u1", "t1", "a.md") is None, "旧字节已经挪走，drop 之后不该复活"
