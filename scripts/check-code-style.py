#!/usr/bin/env python3
"""补充检查格式化工具未覆盖的导入位置与静态 HTML 结构。"""

from __future__ import annotations

import ast
import sys
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# resource 仅在 POSIX 提供；其余标准库必须在模块顶部导入。
PLATFORM_MODULES = frozenset({"resource", "fcntl", "msvcrt", "winreg", "pwd", "grp", "termios"})


def python_problems(source: str, *, check_lazy_reasons: bool = False) -> list[str]:
    """返回源码的行宽与导入问题；业务模块还要求说明每组延迟导入的原因。"""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    lines = source.splitlines()
    problems = []
    # Ruff 会豁免单个不可拆词；补查连续中文、正则和 SQL 的实际显示宽度。
    for number, line in enumerate(lines, 1):
        width = sum(
            0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
            for char in line.expandtabs(4)
        )
        if width > 110 and "noqa: E501" not in line and "https://" not in line and "http://" not in line:
            problems.append(f"{number}：显示宽度 {width} 超过 110 列，请折行且保持字面量值不变")

    def documented(node: ast.AST) -> bool:
        """连续的导入语句可以共用前面的原因注释。"""
        for line in reversed(lines[max(0, node.lineno - 4) : node.lineno - 1]):
            if "延迟导入：" in line and line.partition("延迟导入：")[2].strip():
                return True
            if line.strip() and not line.lstrip().startswith("#"):
                break
        parent = parents.get(node)
        if parent is not None:
            for _, values in ast.iter_fields(parent):
                if isinstance(values, list) and node in values:
                    index = values.index(node)
                    if index and isinstance(values[index - 1], (ast.Import, ast.ImportFrom)):
                        return documented(values[index - 1])
        return False

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.level:
            problems.append(f"{node.lineno}：使用包内绝对短导入，避免相对导入与双模块身份")
            continue
        modules = (
            [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
        )
        if any(name.startswith("mindex.") for name in modules):
            problems.append(f"{node.lineno}：包内组件应使用短导入，避免双模块身份")
        ancestor = node
        local = False
        while ancestor in parents:
            ancestor = parents[ancestor]
            if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local = True
                break
        for module in modules:
            head = module.split(".")[0]
            if local and head in sys.stdlib_module_names and head not in PLATFORM_MODULES:
                problems.append(f"{node.lineno}：标准库 {module} 应提升到模块顶部")
        if local and check_lazy_reasons and not documented(node):
            problems.append(f"{node.lineno}：延迟导入须用“延迟导入：原因”说明可选依赖、循环或装配约束")
    return problems


class PageChecker(HTMLParser):
    """检查静态页面的重复属性、重复 ID、编码声明与结构样式分离。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line = self.getpos()[0]
        duplicates = [name for name, count in Counter(name for name, _ in attrs).items() if count > 1]
        if duplicates:
            self.problems.append(f"{line}：{tag} 存在重复属性 {duplicates}")
        values = dict(attrs)
        identity = values.get("id")
        if identity:
            if identity in self.ids:
                self.problems.append(f"{line}：重复 ID {identity}")
            self.ids.add(identity)
        if "style" in values:
            self.problems.append(f"{line}：静态 style 应迁入所属页面的 CSS 类")
        if tag == "img" and "alt" not in values:
            self.problems.append(f"{line}：img 缺少 alt 文本（装饰图片使用空 alt）")


def html_problems(source: str) -> list[str]:
    """返回静态业务页面的结构、编码与样式分离问题。"""
    checker = PageChecker()
    checker.feed(source)
    if not source.lstrip().lower().startswith("<!doctype html>"):
        checker.problems.append("1：页面必须声明 HTML5 doctype")
    if '<meta charset="utf-8"' not in source.encode("utf-8")[:1024].decode("utf-8", errors="ignore"):
        checker.problems.append("1：前 1024 字节内必须声明 utf-8 编码")
    charset = source.find('<meta charset="utf-8"')
    script = source.find("<script")
    if script >= 0 and (charset < 0 or charset > script):
        checker.problems.append("1：编码声明必须位于脚本之前")
    return checker.problems


def main() -> int:
    """检查仓库中的维护源码，存在问题时返回非零退出码。"""
    problems = []
    for directory in ("backend/src", "tests", "scripts", "eval"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            problems.extend(
                f"{path.relative_to(ROOT)}:{problem}"
                for problem in python_problems(
                    path.read_text(encoding="utf-8"), check_lazy_reasons=directory == "backend/src"
                )
            )
    # 渲染模板与 Electron 自包含启动页有独立运行约束，不适用静态业务页面的样式分离规则。
    for path in sorted((ROOT / "frontend/src").rglob("*.html")):
        problems.extend(
            f"{path.relative_to(ROOT)}:{problem}"
            for problem in html_problems(path.read_text(encoding="utf-8"))
        )
    print("\n".join(problems) if problems else "补充代码规范检查通过（导入位置、静态 HTML 结构）")
    return bool(problems)


if __name__ == "__main__":
    raise SystemExit(main())
