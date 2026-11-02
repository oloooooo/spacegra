"""上传服务里不碰 IO 的那部分：文件名校验与单文件大小上限（ADR-0018）。

真正的写路径（`save_upload`）要对象存储 + PG，留到 e2e；这里只钉住两条安全底线：
路径分隔符必须被拒（否则 key 会逃出 `uploads/<owner>/<thread>/`），以及表格与普通文件
用的是**不同的**配置项（表格大表上限单独可调）。
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

    async def execute(self, stmt):  # noqa: ANN001
        if self.fail_execute:
            raise RuntimeError("db down")

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


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
