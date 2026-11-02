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
