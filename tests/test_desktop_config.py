"""桌面配置模板：严格校验通过，相对路径按配置所在目录（用户数据目录）解析。"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from mindex.shared.config import load_config

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "desktop" / "config.template.yaml"


def test_template_validates_and_resolves_inside_user_data(tmp_path):
    user = tmp_path / "Application Support" / "Mindex"
    user.mkdir(parents=True)
    shutil.copy(TEMPLATE, user / "config.yaml")
    config = load_config(user / "config.yaml")
    assert config.data_dir == (user / "data").resolve()
    assert Path(config.studio.subtitle.font_file) == (user / "fonts" / "NotoSansCJKsc-Regular.otf").resolve()
    assert config.studio.subtitle.font_family == "Noto Sans CJK SC"
    for url in (config.embedding.base_url, config.asr.base_url, config.studio.tts.base_url):
        assert url.startswith("http://127.0.0.1:"), url
    assert config.detector.backend == "adaptive"  # 不依赖模型服务即可 prepare


def test_template_ports_match_run_scripts():
    """模板里的本机端口与 scripts/run-*.sh 的默认端口一致。"""
    def default_port(script, variable):
        text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
        return int(re.search(rf"\${{{variable}:-(\d+)}}", text).group(1))

    config = load_config(TEMPLATE)
    assert f":{default_port('run-wemm.sh', 'MINDEX_WEMM_PORT')}/" in config.embedding.base_url
    assert f":{default_port('run-qwen3-asr.sh', 'MINDEX_ASR_PORT')}/" in config.asr.base_url
    assert f":{default_port('run-tts-voiceclone.sh', 'MINDEX_TTS_PORT')}/" in config.studio.tts.base_url


def test_desktop_version_matches_backend():
    package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert package["version"] == re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)
