"""根目录 README 截图：自动连播产物与引用的一致性。"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs/assets"
READMES = (ROOT / "README.md", ROOT / "README.zh-CN.md")
IMAGE_REF = re.compile(r"\]\((docs/assets/[^)\s]+)\)")
LINKED_IMAGE = re.compile(r'<a href="docs/assets/[^"]+"><img src=')


def test_gallery_artifacts_in_sync():
    script = ROOT / "scripts/build-readme-gallery.py"
    checked = subprocess.run([sys.executable, str(script), "--check"], check=False, capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_readme_references_existing_images():
    for readme in READMES:
        text = readme.read_text(encoding="utf-8")
        refs = set(IMAGE_REF.findall(text))
        assert refs, readme.name
        for ref in refs:
            assert (ROOT / ref).is_file(), f"{readme.name}：{ref} 不存在"
        # 截图不再并排排列缩略图，改为自动连播图 + 逐张原图清单。
        assert 'width="150"' not in text, readme.name
        assert not LINKED_IMAGE.search(text), readme.name


def test_gallery_groups_listed_in_both_readmes():
    manifest = json.loads((ASSETS / "gallery.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "webp"
    assert manifest["groups"]
    for readme in READMES:
        text = readme.read_text(encoding="utf-8")
        for group in manifest["groups"]:
            assert f"docs/assets/{group['output']}" in text, f"{readme.name}：缺少 {group['output']}"
            for source in group["sources"]:
                assert f"(docs/assets/{source['file']})" in text, f"{readme.name}：缺少 {source['file']} 的原图链接"
        assert text.count("<summary>") == text.count("</summary>")
