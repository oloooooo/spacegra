"""`shared/session/upload_rule.py` 的纯逻辑测试。

重点锁三件事：md 一律归主 agent、`<空间>_` 前缀按**最长匹配**（空间名可含下划线）、
**越权前缀必须降级**到通用 xlsx agent（文件名不是权限来源）。
"""

from __future__ import annotations

import pytest

from shared.session.upload_rule import (
    MAIN_AGENT,
    XLSX_AGENT,
    is_xlsx,
    match_space,
    route_upload,
)

SPACES = frozenset({"消融实验", "消融实验_旧", "a_b", "a"})


class TestIsXlsx:
    @pytest.mark.parametrize("name", ["t.xlsx", "T.XLSX", "表格.Xlsx"])
    def test_suffix_any_case(self, name: str) -> None:
        assert is_xlsx(name) is True

    @pytest.mark.parametrize("name", ["t.md", "t.xls", "txlsx", "xlsx", "t.xlsx.bak"])
    def test_not_xlsx(self, name: str) -> None:
        assert is_xlsx(name) is False


class TestMatchSpace:
    def test_exact_prefix(self) -> None:
        assert match_space("消融实验_表1.xlsx", SPACES) == "消融实验"

    def test_longest_prefix_wins(self) -> None:
        # 空间名自带下划线：不能按第一个下划线切
        assert match_space("消融实验_旧_表1.xlsx", SPACES) == "消融实验_旧"
        assert match_space("a_b_表.xlsx", SPACES) == "a_b"
        assert match_space("a_表.xlsx", SPACES) == "a"

    def test_no_prefix(self) -> None:
        assert match_space("表1.xlsx", SPACES) is None

    def test_unauthorized_space(self) -> None:
        assert match_space("秘密空间_表1.xlsx", SPACES) is None

    def test_prefix_with_empty_stem_still_matches(self) -> None:
        # `消融实验_.xlsx` 的表名部分是空的：虽然畸形，但前缀确实带着空间名，
        # 认它比另开一条分支简单（它也能正常落到那个空间 agent 手里）。
        assert match_space("消融实验_.xlsx", SPACES) == "消融实验"

    def test_name_is_exactly_the_prefix(self) -> None:
        # `消融实验_`（连扩展名都没有）：不算匹配
        assert match_space("消融实验_", SPACES) is None

    def test_spaces_none_or_empty(self) -> None:
        assert match_space("消融实验_表1.xlsx", None) is None
        assert match_space("消融实验_表1.xlsx", frozenset()) is None


class TestRouteUpload:
    @pytest.mark.parametrize("name", ["note.md", "README.md", "notes.txt", "data.csv"])
    def test_non_xlsx_goes_main(self, name: str) -> None:
        assert route_upload(name, SPACES) == MAIN_AGENT

    def test_authorized_space_xlsx_goes_to_that_agent(self) -> None:
        assert route_upload("消融实验_表1.xlsx", SPACES) == "消融实验"
        assert route_upload("消融实验_旧_表1.xlsx", SPACES) == "消融实验_旧"

    def test_unauthorized_space_falls_back_to_generic_xlsx(self) -> None:
        # 用户 A 传了 秘密空间_xxx.xlsx：不报错、不分配，只交给通用 agent
        assert route_upload("秘密空间_表1.xlsx", SPACES) == XLSX_AGENT

    def test_bare_xlsx_falls_back_to_generic_xlsx(self) -> None:
        assert route_upload("表1.xlsx", SPACES) == XLSX_AGENT

    def test_spaces_none_still_goes_generic(self) -> None:
        # 本地无鉴权模式：没有空间清单就不猜
        assert route_upload("消融实验_表1.xlsx", None) == XLSX_AGENT

    def test_path_separators_never_route_into_a_space(self) -> None:
        assert route_upload("消融实验_../etc/passwd.xlsx", SPACES) == XLSX_AGENT
        assert route_upload("a\\b.xlsx", SPACES) == XLSX_AGENT

    def test_case_insensitive_suffix_routes_as_xlsx(self) -> None:
        assert route_upload("消融实验_表1.XLSX", SPACES) == "消融实验"
        assert route_upload("表1.XLSX", SPACES) == XLSX_AGENT
