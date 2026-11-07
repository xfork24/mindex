"""打包清单必须覆盖全部源码子包，避免新增目录后漏打包。"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def declared() -> set[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(config["tool"]["setuptools"]["packages"])


def test_backend_packages_are_declared():
    src = ROOT / "backend/src"
    found = {".".join(("mindex", *path.parent.relative_to(src).parts)) for path in src.rglob("*/__init__.py")}
    assert {"mindex.studio.workspace", "mindex.studio.workspace.handlers"} <= found  # 嵌套子包同样要声明
    assert found | {"mindex"} <= declared()


def test_studio_template_data_is_packaged():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "templates/*.yaml" in config["tool"]["setuptools"]["package-data"]["mindex.studio"]
    assert len(list((ROOT / "backend/src/studio/templates").glob("*.yaml"))) == 6
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include backend/src *.py *.yaml" in manifest


def test_frontend_page_groups_are_declared():
    found = {f"mindex.web.{path.name}" for path in (ROOT / "frontend/src").iterdir() if path.is_dir()}
    assert found
    assert found | {"mindex.web"} <= declared()


def test_manifest_lists_only_existing_top_level_files():
    for line in (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines():
        if line.startswith("include "):
            for name in line.split()[1:]:
                assert (ROOT / name).is_file(), name
