"""上传服务里不碰 IO 的那部分：文件名校验与单文件大小上限（ADR-0018）。

真正的写路径（`save_upload`）要对象存储 + PG，留到 e2e；这里只钉住两条安全底线：
路径分隔符必须被拒（否则 key 会逃出 `uploads/<owner>/<thread>/`），以及表格与普通文件
用的是**不同的**配置项（表格大表上限单独可调）。
"""

from __future__ import annotations

import pytest

from custom.service import uploads


class TestSafeName:
    def test_keeps_chinese_and_spaces(self) -> None:
        assert uploads.safe_name("  消融实验 1.xlsx  ") == "消融实验 1.xlsx"

    @pytest.mark.parametrize("name", ["", "   ", ".", ".."])
    def test_rejects_empty_or_dot_names(self, name: str) -> None:
        with pytest.raises(uploads.UploadError) as e:
            uploads.safe_name(name)
        assert e.value.status_code == 422

    @pytest.mark.parametrize("name", ["a/b.xlsx", "a\\b.xlsx", "a\x00b.xlsx", "../x.xlsx"])
    def test_rejects_path_separators(self, name: str) -> None:
        with pytest.raises(uploads.UploadError) as e:
            uploads.safe_name(name)
        assert "路径分隔符" in e.value.message
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


class TestReservedNames:
    """#14：Windows 设备名与本服务的临时文件前缀一律 422。"""

    @pytest.mark.parametrize(
        "name",
        ["CON", "con.txt", "NUL.xlsx", "COM1", "lpt9.json", "CON .txt", ".tmp-a.xlsx", ".TMP-a", ".bak-a.xlsx"],
    )
    def test_rejected(self, name: str) -> None:
        with pytest.raises(uploads.UploadError) as e:
            uploads.safe_name(name)
        assert e.value.status_code == 422

    @pytest.mark.parametrize("name", ["console.txt", "tmp-a.xlsx", "commander.md", "备份.bak"])
    def test_similar_names_still_pass(self, name: str) -> None:
        assert uploads.safe_name(name) == name


class _FakeStore:
    """内存版对象存储：够 `save_upload` 走一遍备份/覆盖/回滚。"""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.fail_commit = False

    def stat(self, key: str) -> dict | None:
        return {"Key": key} if key in self.objects else None

    def copy(self, src: str, dst: str) -> None:
        self.objects[dst] = self.objects[src]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def put_stream(self, src, key, *, max_bytes: int) -> tuple[str, int]:
        data = src.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise uploads.blobs.BlobTooLarge(max_bytes)
        self.objects[key] = data
        return ("sha-new", len(data))


class _FakeSession:
    def __init__(self, *, fail_commit: bool) -> None:
        self.fail_commit = fail_commit
        self.commits = 0
        self.executed: list[str] = []

    async def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        self.executed.append(str(stmt))

    async def scalar(self, stmt):  # noqa: ANN001, ANN201 - 同名文件已存在
        return "old-sha"

    async def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("db down")
        self.commits += 1

    async def rollback(self) -> None:
        return None


def _wire(monkeypatch: pytest.MonkeyPatch, store: _FakeStore) -> None:
    for name in ("stat", "copy", "delete", "put_stream"):
        monkeypatch.setattr(uploads.blobs, name, getattr(store, name))
    monkeypatch.setattr(
        uploads.config,
        "get",
        lambda key: {"session_files.max_files_per_session": 50, "session_files.max_upload_mb": 5}[key],
    )


class TestOverwriteRollback:
    """#7：覆盖上传失败时旧字节必须回来，成功时备份要清掉。"""

    async def test_failed_commit_restores_old_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _FakeStore({"uploads/u1/t1/a.md": b"old"})
        _wire(monkeypatch, store)

        with pytest.raises(RuntimeError):
            await uploads.save_upload(
                _FakeSession(fail_commit=True),
                owner="u1",
                thread_id="t1",
                name="a.md",
                src=__import__("io").BytesIO(b"new"),
                spaces=[],
            )

        assert store.objects == {"uploads/u1/t1/a.md": b"old"}

    async def test_success_drops_backup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _FakeStore({"uploads/u1/t1/a.md": b"old"})
        _wire(monkeypatch, store)
        session = _FakeSession(fail_commit=False)

        await uploads.save_upload(
            session, owner="u1", thread_id="t1", name="a.md", src=__import__("io").BytesIO(b"new"), spaces=[]
        )

        assert store.objects == {"uploads/u1/t1/a.md": b"new"}
        assert session.commits == 1
