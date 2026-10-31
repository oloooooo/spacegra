"""LocalBackend：一趟 scandir 的快速路径，结果必须与 FilesystemBackend 等价。

回归重点（都是 2026-10 基准里踩出来的）：

1. ``ls``/``glob`` 的 path / is_dir / size 必须和父类逐条一致——快不能靠丢信息换；
2. ``/../x`` 这类越界输入必须变成错误字符串。父类只 catch ``OSError``/``RuntimeError``，
   ``_resolve_path`` 抛的 ``ValueError`` 会直接冒泡到中间件（实测）；
3. 符号链接一律不跟、不出现在结果里：confinement 由构造保证，而不是逐条 ``resolve()``；
4. 二进制文件的 ``read`` 给明确错误，别把 ``UnicodeDecodeError`` 甩出去；
5. 默认只读，写操作返回拒绝而不是抛异常。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from deepagents.backends import FilesystemBackend

from graphs.access.local_backend import LocalBackend


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """根下：普通文件、点文件、空文件、二进制、两层子目录、空目录。"""
    (tmp_path / "a.md").write_text("# a\nfirst\nsecond\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("bee\n", encoding="utf-8")
    (tmp_path / ".hidden.md").write_text("hidden\n", encoding="utf-8")
    (tmp_path / "empty.md").write_text("", encoding="utf-8")
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    # GBK 编码的中文：不是合法 UTF-8，父类 read 会在这里抛 UnicodeDecodeError
    (tmp_path / "gbk.dat").write_bytes(b"\xc7\xe9\xb1\xa8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.md").write_text("see a.md needle\n", encoding="utf-8")
    (tmp_path / "sub" / "deep").mkdir()
    (tmp_path / "sub" / "deep" / "d.md").write_text("deep needle\n", encoding="utf-8")
    (tmp_path / "blank").mkdir()
    return tmp_path


@pytest.fixture
def pair(tree: Path):
    return FilesystemBackend(root_dir=str(tree), virtual_mode=True), LocalBackend(str(tree))


def _glob_keys(result):
    """glob 结果归一成可比集合（modified_at 用字符串，同一个文件必然相同）。"""
    assert result.error is None, result.error
    return {(m["path"], m["size"], m["modified_at"]) for m in result.matches}


def _ls_keys(result):
    """目录项不比 modified_at：NTFS 会延迟刷新目录时间戳，
    两次读取（scandir 的枚举快照 vs 直接 stat）可能相差几百微秒。"""
    assert result.error is None, result.error
    return {
        (e["path"], e["is_dir"], e["size"], None if e["is_dir"] else e["modified_at"])
        for e in result.entries
    }


@pytest.mark.parametrize("pattern", ["*.md", "**/*.md", "**/*.txt", "sub/**/*.md", "*", "**/*"])
def test_glob_matches_base(pair, pattern):
    """同一棵树、同一个 matcher，结果集合必须逐条相同。"""
    base, fast = pair
    assert _glob_keys(fast.glob(pattern)) == _glob_keys(base.glob(pattern))


@pytest.mark.parametrize("path", ["/", "/sub", "/sub/deep", "/blank"])
def test_ls_matches_base(pair, path):
    base, fast = pair
    assert _ls_keys(fast.ls(path)) == _ls_keys(base.ls(path))


def test_ls_errors_match_base(pair):
    base, fast = pair
    assert fast.ls("/nope").error == base.ls("/nope").error == "Path '/nope': path_not_found"
    assert fast.ls("/a.md").error == base.ls("/a.md").error == "Path '/a.md': not_a_directory"


def test_glob_unknown_dir_is_empty_not_error(pair):
    base, fast = pair
    assert fast.glob("*.md", "/nope").matches == base.glob("*.md", "/nope").matches == []


@pytest.mark.parametrize("args", [(), (1, 1), (0, 2)])
def test_read_text_matches_base(pair, args):
    base, fast = pair
    assert fast.read("/a.md", *args).file_data == base.read("/a.md", *args).file_data


def test_read_empty_file_matches_base(pair):
    base, fast = pair
    assert fast.read("/empty.md").file_data == base.read("/empty.md").file_data


def test_read_non_utf8_returns_explicit_error(pair):
    """非 UTF-8 字节流：父类把 UnicodeDecodeError 直接扇出去，本类给可行下一步。"""
    base, fast = pair
    assert "can't decode" in (base.read("/gbk.dat").error or "")  # 父类的原始症状
    err = fast.read("/gbk.dat").error or ""
    assert "binary" in err
    assert "not valid UTF-8" in err
    assert "read_xlsx" in err  # 给 agent 一条可执行的下一条指令
    assert fast.read("/gbk.dat").file_data is None


def test_read_nul_bytes_detected_even_if_utf8(pair):
    """UTF-8 合法但含 NUL 的字节流也算二进制（tar/二进制日志的常见形态）。

    NUL 字节在 UTF-8 里是合法码点，父类会把它当文本原样返回给模型（实测 error=None），
    所以这里对父类只记录现状，不要求它报错。
    """
    base, fast = pair
    assert base.read("/bin.dat").error is None
    assert "\x00" in base.read("/bin.dat").file_data["content"]  # 原样把 NUL 丢给模型
    err = fast.read("/bin.dat").error or ""
    assert "binary" in err and "NUL" in err
    assert fast.read("/bin.dat").file_data is None


def test_read_size_cap(tree):
    err = LocalBackend(str(tree), max_read_bytes=4).read("/a.md").error or ""
    assert "too large to read" in err


@pytest.mark.parametrize(
    ("call", "needle"),
    [
        (lambda be: be.read("/../../etc/passwd"), "Path traversal not allowed"),
        (lambda be: be.ls("/.."), "Path traversal not allowed"),
        (lambda be: be.glob("*.md", "/../.."), "Path traversal not allowed"),
    ],
)
def test_traversal_is_error_not_exception(tree, call, needle):
    """父类会让 ValueError 冒泡；本类必须把它变成结构化错误。"""
    base = FilesystemBackend(root_dir=str(tree), virtual_mode=True)
    with pytest.raises(ValueError):
        call(base)  # 记录上游现状：这里抛异常
    result = call(LocalBackend(str(tree)))
    assert result.error and needle in result.error


def test_refused_glob_pattern_is_error(tree):
    result = LocalBackend(str(tree)).glob("../*.md")
    assert result.error and "Path traversal not allowed" in result.error
    assert result.matches is None


def test_glob_budget_truncates(tree):
    result = LocalBackend(str(tree), glob_timeout=0.0).glob("**/*")
    assert result.truncated is True
    assert result.truncation_reason == "budget"
    assert result.matches == []


def test_symlinks_are_skipped(tree):
    """根内的符号链接一律不跟。

    父类逐条 ``resolve()``，于是 ``/link.md`` 被“透明”成它指向的 ``/a.md``——
    同一个文件出现两次（实测）。本类在扫描时看 ``is_symlink()`` 直接跳过，
    所以既不会把链接解析成别人，也不会重复收录。
    """
    try:
        os.symlink(tree / "a.md", tree / "link.md")
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    base = FilesystemBackend(root_dir=str(tree), virtual_mode=True)
    fast = LocalBackend(str(tree))
    base_paths = [m["path"] for m in base.glob("*.md").matches]
    fast_paths = [m["path"] for m in fast.glob("*.md").matches]

    assert "/link.md" not in base_paths  # 父类把它解析成了 /a.md
    assert "/link.md" not in fast_paths
    assert base_paths.count("/a.md") == 2  # 父类的重复项
    assert fast_paths.count("/a.md") == 1
    # ls 侧同样：链接条目不可见，所以根目录项数不膨胀
    assert "/link.md" not in {e["path"] for e in fast.ls("/").entries}
    assert [e["path"] for e in fast.ls("/").entries].count("/a.md") == 1


def test_write_paths_are_refused_by_default(tree):
    backend = LocalBackend(str(tree))
    assert "read-only" in (backend.write("/new.md", "x").error or "")
    assert "read-only" in (backend.edit("/a.md", "a", "b").error or "")
    assert "read-only" in (backend.delete("/a.md").error or "")
    uploaded = backend.upload_files([("/new.md", b"x")])
    assert uploaded[0].error and "read-only" in uploaded[0].error
    assert not (tree / "new.md").exists()


def test_allow_write_delegates_to_base(tree):
    backend = LocalBackend(str(tree), allow_write=True)
    assert backend.write("/new.md", "hello\n").error is None
    assert (tree / "new.md").read_text(encoding="utf-8") == "hello\n"
    assert backend.edit("/new.md", "hello", "bye").error is None
    assert (tree / "new.md").read_text(encoding="utf-8") == "bye\n"
    assert backend.delete("/new.md").error is None
    assert not (tree / "new.md").exists()


def test_virtual_paths_do_not_leak_root(tree):
    """virtual_mode 下对外只暴露 /xxx，不能把真实 root 拼进结果。"""
    result = LocalBackend(str(tree)).ls("/")
    assert all(e["path"].startswith("/") for e in result.entries)
    assert all(str(tree) not in e["path"] for e in result.entries)
