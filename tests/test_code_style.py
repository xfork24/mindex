"""补充规范检查必须发现真实的导入与 HTML 缺陷。"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("code_style", ROOT / "scripts/check-code-style.py")
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


@pytest.mark.parametrize(
    "source,problem",
    [
        ("def run():\n    import json\n", "标准库 json"),
        ("from mindex.shared.config import Config\n", "短导入"),
        ("from .shared import config\n", "相对导入"),
    ],
)
def test_python_import_defects(source, problem):
    assert any(problem in item for item in CHECKER.python_problems(source))


def test_module_imports_and_optional_platform_dependency_are_valid():
    assert CHECKER.python_problems("import json\ndef run():\n    import resource\n") == []


def test_display_width_counts_chinese_columns_and_allows_urls():
    assert any("显示宽度" in item for item in CHECKER.python_problems("# " + "超" * 60))
    assert CHECKER.python_problems("# https://example.test/" + "x" * 120) == []


def test_lazy_business_import_needs_a_reason_and_grouped_imports_share_it():
    plain = "def run():\n    from shared import storage\n"
    assert CHECKER.python_problems(plain, check_lazy_reasons=True)
    documented = (
        "def run():\n"
        "    # 延迟导入：配置加载与存储装配存在循环，运行时再解析。\n"
        "    from shared import storage\n"
        "    from shared import config\n"
    )
    assert CHECKER.python_problems(documented, check_lazy_reasons=True) == []


@pytest.mark.parametrize(
    "body,problem",
    [
        ('<form class="panel" class="login"></form>', "重复属性"),
        ('<div id="entry"></div><span id="entry"></span>', "重复 ID"),
        ('<textarea style="width:100%"></textarea>', "style"),
        ('<img src="sample.jpg">', "alt"),
    ],
)
def test_html_structure_defects(body, problem):
    html = '<!doctype html><html><head><meta charset="utf-8"></head><body>' + body + "</body></html>"
    assert any(problem in item for item in CHECKER.html_problems(html))


def test_encoding_precedes_scripts_and_is_early():
    early = '<!doctype html><meta charset="utf-8"><script src="a.js"></script>'
    assert CHECKER.html_problems(early) == []
    assert CHECKER.html_problems('<!doctype html><script src="a.js"></script><meta charset="utf-8">')
    assert CHECKER.html_problems("<!doctype html><!--" + "空" * 400 + '--><meta charset="utf-8">')
