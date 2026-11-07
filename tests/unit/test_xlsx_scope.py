"""xlsx 工具的空间隔离判定（`_check_scope`）。

回归重点：`/空间A/../空间B/x.xlsx` 的首段属于 A（有权限），但真正被打开的是 B 的文件。
只比对原始字符串就等于不设防（2026-10-25 实测可通过，`knowledge/secret/` 真实存在）。
"""

import pytest

from graphs.tools import xlsx as xlsx_mod


@pytest.fixture
def okf(tmp_path, monkeypatch):
    """root 下两个空间 + 一个根散文件 + 一个"别人的"目录。"""
    for name in ("space-a", "space-b", "secret"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "t.xlsx").write_bytes(b"x")
    (tmp_path / "process.md").write_text("hi", encoding="utf-8")
    monkeypatch.setattr(xlsx_mod, "_root", lambda: tmp_path)
    return tmp_path


def _cfg(*spaces: str) -> dict:
    return {"configurable": {"langgraph_auth_user": {"identity": "u1", "user_spaces": list(spaces)}}}


def test_allows_own_space(okf):
    xlsx_mod._check_scope("/space-a/t.xlsx", _cfg("space-a"))


def test_allows_root_file(okf):
    xlsx_mod._check_scope("/process.md", _cfg("space-a"))


def test_allows_dot_segment_inside_own_space(okf):
    xlsx_mod._check_scope("/space-a/./t.xlsx", _cfg("space-a"))


def test_denies_other_space(okf):
    with pytest.raises(ValueError, match="无权访问"):
        xlsx_mod._check_scope("/space-b/t.xlsx", _cfg("space-a"))


def test_denies_missing_root_file(okf):
    with pytest.raises(ValueError, match="无权访问"):
        xlsx_mod._check_scope("/nope.md", _cfg("space-a"))


@pytest.mark.parametrize(
    "path",
    [
        "/space-a/../secret/t.xlsx",  # 换到没权限的目录
        "/space-a/../space-b/t.xlsx",  # 换到另一个空间（同样是别人的）
        "/space-a//../secret/t.xlsx",  # 多斜杠变体
        "/space-a/./../secret/t.xlsx",
    ],
)
def test_denies_traversal_into_other_dir(okf, path):
    with pytest.raises(ValueError, match="无权访问"):
        xlsx_mod._check_scope(path, _cfg("space-a"))


def test_denies_escape_above_root(okf):
    with pytest.raises(ValueError, match="路径越界"):
        xlsx_mod._check_scope("/space-a/../../pyproject.toml", _cfg("space-a"))


def test_denies_symlink_escape(okf):
    """空间里的符号链接指到根外，解析后越界；Windows 上建不了就跳过。"""
    link = okf / "space-a" / "out.xlsx"
    try:
        link.symlink_to(okf.parent / "outside.xlsx")
    except (OSError, NotImplementedError):
        pytest.skip("该平台不支持创建符号链接")
    with pytest.raises(ValueError, match="路径越界"):
        xlsx_mod._check_scope("/space-a/out.xlsx", _cfg("space-a"))


def test_relative_path_denied(okf):
    with pytest.raises(ValueError, match="必须用以 / 开头"):
        xlsx_mod._check_scope("space-a/t.xlsx", _cfg("space-a"))


def test_no_auth_context_allows(okf):
    """本地 demo（无 langgraph_auth_user）不设限，与改动前一致。"""
    xlsx_mod._check_scope("/secret/t.xlsx", {"configurable": {}})


def test_service_identity_without_forwarded_spaces_denies(okf):
    """服务身份但没转发名单：fail-closed（空集），一个空间都不放行。"""
    cfg = {"configurable": {"langgraph_auth_user": {"identity": "subagent_service"}}}
    with pytest.raises(ValueError, match="无权访问"):
        xlsx_mod._check_scope("/space-a/t.xlsx", cfg)


def test_uploads_allowed(okf):
    """`/uploads/<name>` 是本会话的上传目录，上传时已按会话鉴权，工具层放行。"""
    xlsx_mod._check_scope("/uploads/碳排放清单.xlsx", _cfg())


def test_uploads_nested_path_denied(okf):
    """只允许一层文件名；多层会被 `..` 藏东西。"""
    with pytest.raises(ValueError, match="只能是一层"):
        xlsx_mod._check_scope("/uploads/sub/x.xlsx", _cfg("space-a"))


def test_uploads_traversal_still_denied(okf):
    """放行只看解析后的首段：`/uploads/../space-b/x.xlsx` 真开的是 space-b。"""
    with pytest.raises(ValueError, match="无权访问"):
        xlsx_mod._check_scope("/uploads/../space-b/t.xlsx", _cfg("space-a"))
