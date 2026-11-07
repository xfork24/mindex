"""页面引用的 /assets/ 版本参数必须是被引用文件的内容哈希（由 scripts/build-assets.py 维护）。"""

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build-assets.py"
WEB = ROOT / "frontend/src"


def test_asset_versions_match_content():
    checked = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], check=False, capture_output=True, text=True
    )
    assert checked.returncode == 0, checked.stderr


def test_every_page_asset_carries_its_content_hash():
    pages = sorted(WEB.glob("*/*.html"))
    assert {p.name for p in pages} >= {
        "index.html",
        "admin.html",
        "persons.html",
        "settings.html",
        "studio.html",
        "login.html",
    }
    for page in pages:
        for path, version in re.findall(
            r'(?:src|href)="/assets/([^"?#]+)(?:\?v=([^"#]*))?"', page.read_text()
        ):
            expected = hashlib.sha256((WEB / path).read_bytes()).hexdigest()[:10]
            assert version == expected, (page.name, path)


def test_check_reports_stale_versions_without_writing(tmp_path):
    # 复制脚本与一份最小前端目录，验证 --check 只报告不改写、默认模式改写为内容哈希。
    web = tmp_path / "frontend/src"
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/build-assets.py").write_text(SCRIPT.read_text())
    (web / "shared").mkdir(parents=True)
    (web / "demo").mkdir()
    (web / "shared/x.js").write_text("console.log(1);\n")
    page = web / "demo/demo.html"
    page.write_text('<script src="/assets/shared/x.js?v=old" defer></script>')
    script = [sys.executable, str(tmp_path / "scripts/build-assets.py")]
    stale = subprocess.run([*script, "--check"], check=False, capture_output=True, text=True)
    assert stale.returncode == 1 and "demo.html" in stale.stderr
    assert "?v=old" in page.read_text()
    assert subprocess.run(script, check=False, capture_output=True, text=True).returncode == 0
    digest = hashlib.sha256(b"console.log(1);\n").hexdigest()[:10]
    assert f'/assets/shared/x.js?v={digest}"' in page.read_text()
    assert subprocess.run([*script, "--check"], check=False, capture_output=True, text=True).returncode == 0
    page.write_text('<script src="/assets/shared/missing.js"></script>')
    missing = subprocess.run([*script, "--check"], check=False, capture_output=True, text=True)
    assert missing.returncode == 1 and "missing.js" in missing.stderr
