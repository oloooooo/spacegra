"""布局护栏：三个包的依赖方向与 aegra.json 的入口必须始终成立。

2026-10 的重构把代码分成三层（见 CONTEXT.md「代码布局」）：

    custom/   HTTP 面（API、鉴权、发件箱分发）
    graphs/   图面（run 里跑的 deep agents）
    shared/   两侧共用的纯逻辑

规则只有三条，但违反任何一条都会让「文件该放哪」重新变成猜谜，所以用测试钉住：

1. custom 不许 import graphs（HTTP 启动路径不能拖进整张图）；
2. shared 不许 import 任何一侧（它是两侧的公共底座，不能反向依赖）；
3. graphs 一行例外也没有了（原来唯一一处是 xlsx 懒加载会话文件元数据，
   已随上传功能移除）。

另外校验 aegra.json 里声明的入口文件与导出名真的存在——重命名漏改配置会在这里报错，
而不是等到启动时。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# graphs -> custom 的唯一例外：相对导入的模块前缀
GRAPHS_ALLOWED: set[str] = set()  # graphs 不该 import custom（session_files 已随上传功能移除）


def _imports(path: Path) -> set[str]:
    """文件里所有 `import X` / `from X import ...` 的模块名（只取顶层包名够用）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    return mods


def _py_files(pkg: str) -> list[Path]:
    return [p for p in (ROOT / pkg).rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize("pkg", ["custom", "graphs", "shared"])
def test_dependency_direction(pkg: str) -> None:
    # custom 与 graphs 互不 import；shared 谁都不能 import（它是两侧的底座）
    forbidden = {"custom", "graphs"}
    if pkg != "shared":
        forbidden.discard(pkg)
    offenders: list[str] = []
    for path in _py_files(pkg):
        for mod in _imports(path):
            first = mod.split(".")[0]
            if first not in forbidden:
                continue
            if pkg == "graphs" and any(a == mod or a.startswith(mod + ".") for a in GRAPHS_ALLOWED):
                continue
            offenders.append(f"{path.relative_to(ROOT).as_posix()} -> {mod}")
    assert not offenders, f"{pkg}/ 不该 import 这些模块：\n" + "\n".join(offenders)


FRAMEWORK_MODULES = ("fastapi", "langchain", "deepagents")


def test_shared_is_framework_light() -> None:
    """shared/ 不进 HTTP / agent 框架：它会被图的启动路径直接 import。

    ``langgraph.store.base`` 只提供 PutOp 这类数据结构，不算框架依赖。
    """
    offenders: list[str] = []
    for path in _py_files("shared"):
        for mod in _imports(path):
            if mod.split(".")[0] in FRAMEWORK_MODULES:
                offenders.append(f"{path.relative_to(ROOT).as_posix()} -> {mod}")
    assert not offenders, "shared/ 只放纯逻辑：\n" + "\n".join(offenders)


def test_aegra_json_entrypoints_exist() -> None:
    cfg = json.loads((ROOT / "aegra.json").read_text(encoding="utf-8"))
    entries = [*cfg["graphs"].values(), cfg["http"]["app"], cfg["auth"]["path"]]
    for entry in entries:
        path, _, name = entry.partition(":")
        target = ROOT / path
        assert target.is_file(), f"aegra.json 指向不存在的文件：{entry}"
        tree = ast.parse(target.read_text(encoding="utf-8"))
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)}
        defined |= {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        assert name in defined, f"{path} 里没有导出 {name}（aegra.json 写的是 {entry}）"
