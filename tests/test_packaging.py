"""打包清单必须覆盖全部源码子包，避免新增目录后漏打包。"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def declared() -> set[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(config["tool"]["setuptools"]["packages"])


def test_backend_packages_are_declared():
    found = {
        f"mindex.{path.parent.name}"
        for path in (ROOT / "backend/src").glob("*/__init__.py")
    }
    assert found
    assert found | {"mindex"} <= declared()


def test_frontend_page_groups_are_declared():
    found = {
        f"mindex.web.{path.name}"
        for path in (ROOT / "frontend/src").iterdir()
        if path.is_dir()
    }
    assert found
    assert found | {"mindex.web"} <= declared()


def test_manifest_lists_only_existing_top_level_files():
    for line in (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines():
        if line.startswith("include "):
            for name in line.split()[1:]:
                assert (ROOT / name).is_file(), name
