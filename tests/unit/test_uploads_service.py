"""上传服务的校验与写路径保护（ADR-0018）。

前两节是不碰 IO 的纯函数；`TestOverwriteRollback` / `TestSaveUploadGuards` 用 fs 介质 + 假
session 钉住失败路径：台账写不进去要换回旧字节，配额/超限/存储不可用要映射成 4xx/5xx。
"""

from __future__ import annotations

import pytest

from custom.service import uploads
from custom.service import uploads as uploads_service


class TestSafeName:
    def test_keeps_chinese_and_spaces(self) -> None:
        assert uploads.safe_name("  消融实验 1.xlsx  ") == "消融实验 1.xlsx"

    @pytest.mark.parametrize("name", ["", "   ", ".", ".."])
    def test_rejects_empty_or_dot_names(self, name: str) -> None:
        with pytest.raises(uploads.UploadError) as e:
            uploads.safe_name(name)
        assert e.value.status_code == 422

    @pytest.mark.parametrize("name", ["a/b.xlsx", "a\\b.xlsx", "a\x00b.xlsx", "../x.xlsx", "a:b.xlsx", "x."])
    def test_rejects_path_separators(self, name: str) -> None:
        with pytest.raises(uploads.UploadError) as e:
            uploads.safe_name(name)
        assert "不合法" in e.value.message
        assert e.value.status_code == 422

    def test_rejects_overlong_name(self) -> None:
        with pytest.raises(uploads.UploadError):
            uploads.safe_name("好" * 201)


class TestSizeLimit:
    def test_xlsx_uses_xlsx_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "shared.config.get",
            lambda key: {"xlsx.max_blob_mb": 3, "session_files.max_upload_mb": 5}[key],
        )
        assert uploads.size_limit("a.xlsx") == 3 * 1024 * 1024

    def test_other_files_use_session_files_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "shared.config.get",
            lambda key: {"xlsx.max_blob_mb": 3, "session_files.max_upload_mb": 5}[key],
        )
        assert uploads.size_limit("说明.md") == 5 * 1024 * 1024

    def test_uppercase_suffix_counts_as_xlsx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "shared.config.get",
            lambda key: {"xlsx.max_blob_mb": 3, "session_files.max_upload_mb": 5}[key],
        )
        assert uploads.size_limit("A.XLSX") == 3 * 1024 * 1024


class _FakeSession:
    """只实现 `save_upload` 用到的那几个方法。"""

    def __init__(self, *, fail_execute: bool = False) -> None:
        self.fail_execute = fail_execute
        self.commits = 0

    async def scalar(self, stmt):  # noqa: ANN001, ANN201 - 模拟"同名文件已存在"
        return "old-sha"

    async def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if self.fail_execute:
            raise RuntimeError("db down")

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class _QueuedSession(_FakeSession):
    """按顺序返回 `scalar()` 的值：先查同名 sha，再查配额数量；同时记录执行过的 SQL。"""

    def __init__(self, *values: object) -> None:
        super().__init__()
        self.values = list(values)
        self.sql: list[str] = []

    async def scalar(self, stmt):  # noqa: ANN001, ANN201
        return self.values.pop(0)

    async def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        self.sql.append(str(stmt))
        return await super().execute(stmt)


@pytest.fixture
def fs_store(tmp_path, monkeypatch: pytest.MonkeyPatch):
    values = {
        "uploads.dir": str(tmp_path / "uploads"),
        "uploads.store": "fs",
        "session_files.max_upload_mb": 10,
        "xlsx.max_blob_mb": 10,
        "session_files.max_files_per_session": 50,
    }

    def fake_get(key: str):
        return values[key]

    monkeypatch.setattr("custom.service.uploads.config.get", fake_get)
    monkeypatch.setattr("shared.session.uploads.config.get", fake_get)
    return tmp_path / "uploads"


class TestSaveUploadGuards:
    """配额、超限、存储不可用的分支（issue #15）。"""

    async def test_quota_blocks_new_file(self, fs_store) -> None:
        session = _QueuedSession(None, 50)

        with pytest.raises(uploads_service.UploadError) as e:
            await uploads_service.save_upload(
                session, owner="u1", thread_id="t1", name="a.md", src=__import__("io").BytesIO(b"x"), spaces=[]
            )

        assert e.value.status_code == 409
        assert not uploads_service.upload_store.local_path("u1", "t1", "a.md").exists()

    async def test_quota_skipped_for_overwrite(self, fs_store) -> None:
        """已存在的同名文件不算新增，配额满了也能覆盖。"""
        uploads = __import__("shared.session.uploads", fromlist=["uploads"])
        uploads.put_stream(__import__("io").BytesIO(b"old"), owner="u1", thread_id="t1", name="a.md", max_bytes=64)
        session = _QueuedSession("old-sha")

        await uploads_service.save_upload(
            session, owner="u1", thread_id="t1", name="a.md", src=__import__("io").BytesIO(b"new"), spaces=[]
        )

        assert uploads.read_bytes("u1", "t1", "a.md") == b"new"

    async def test_too_large_maps_to_413(self, fs_store, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise uploads_service.upload_store.UploadTooLarge("文件超过 1MB 上限")

        monkeypatch.setattr(uploads_service.upload_store, "put_stream", boom)
        session = _QueuedSession("old-sha")

        with pytest.raises(uploads_service.UploadError) as e:
            await uploads_service.save_upload(
                session, owner="u1", thread_id="t1", name="a.md", src=__import__("io").BytesIO(b"x"), spaces=[]
            )

        assert e.value.status_code == 413

    async def test_store_down_maps_to_503(self, fs_store, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise uploads_service.upload_store.UploadUnavailable("s3 down")

        monkeypatch.setattr(uploads_service.upload_store, "put_stream", boom)
        session = _QueuedSession("old-sha")

        with pytest.raises(uploads_service.UploadError) as e:
            await uploads_service.save_upload(
                session, owner="u1", thread_id="t1", name="a.md", src=__import__("io").BytesIO(b"x"), spaces=[]
            )

        assert e.value.status_code == 503


class TestSameNameSerialization:
    """同一会话的上传要排队：锁 thread 行，也把"发消息"挡在上传之后（issue #13/#16）。"""

    async def test_thread_row_is_locked_before_any_write(self, fs_store) -> None:
        session = _QueuedSession("old-sha")

        await uploads_service.save_upload(
            session, owner="u1", thread_id="t1", name="a.md", src=__import__("io").BytesIO(b"x"), spaces=[]
        )

        assert "FOR UPDATE" in session.sql[0].upper()
        assert len(session.sql) == 2, "锁之后只应再有一条 upsert"
        assert "updated_at" in session.sql[1], "覆盖必须刷新 updated_at（#13）"


class TestOverwriteRollback:
    """覆盖同名文件时台账失败要把旧字节换回来（issue #7）。"""

    async def test_failed_commit_restores_old_bytes(self, fs_store) -> None:
        uploads = __import__("shared.session.uploads", fromlist=["uploads"])

        uploads.put_stream(__import__("io").BytesIO(b"old"), owner="u1", thread_id="t1", name="a.md", max_bytes=64)
        session = _FakeSession(fail_execute=True)

        with pytest.raises(RuntimeError):
            await uploads_service.save_upload(
                session,
                owner="u1",
                thread_id="t1",
                name="a.md",
                src=__import__("io").BytesIO(b"new"),
                spaces=[],
            )

        assert uploads.read_bytes("u1", "t1", "a.md") == b"old", "旧字节必须还在"
        assert [p.name for p in uploads.local_path("u1", "t1", "a.md").parent.iterdir()] == ["a.md"]

    async def test_success_replaces_and_drops_backup(self, fs_store) -> None:
        uploads = __import__("shared.session.uploads", fromlist=["uploads"])

        uploads.put_stream(__import__("io").BytesIO(b"old"), owner="u1", thread_id="t1", name="a.md", max_bytes=64)
        session = _FakeSession()

        await uploads_service.save_upload(
            session, owner="u1", thread_id="t1", name="a.md", src=__import__("io").BytesIO(b"new"), spaces=[]
        )

        assert session.commits == 1
        assert uploads.read_bytes("u1", "t1", "a.md") == b"new"
        assert [p.name for p in uploads.local_path("u1", "t1", "a.md").parent.iterdir()] == ["a.md"]
