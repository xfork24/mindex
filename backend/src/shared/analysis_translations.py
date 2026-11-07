"""旧分析的英文显示补全；原始检查点与发布快照保持不变。"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from shared.analysis_text import select_analysis

HAN = re.compile(r"[\u3400-\u9fff]")


@lru_cache(maxsize=2)
def _load(path: str, mtime: int, namespace=None) -> dict[str, str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        k: v
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str) and v.strip() and not HAN.search(v)
    }


def load_translations(root: Path, *, namespace=None) -> dict[str, str]:
    path = Path(root) / "analysis-translations.en.json"
    try:
        return _load(str(path), path.stat().st_mtime_ns, namespace)
    except FileNotFoundError:
        return {}


def display_metadata(data, root: Path, *, namespace=None):
    """只附带本次响应用到的翻译，由分析专用显示器选择，不改用户文本。"""
    translations = load_translations(root, namespace=namespace)
    if not translations or not isinstance(data, dict):
        return data
    used = {}

    def visit(value):
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            # 同时覆盖 annotation 原值及 views.analysis / matched_text 的标签行。
            for text in [value, *re.split(r"[\n；]", value)]:
                text = text.strip()
                if text in translations:
                    used[text] = translations[text]
                if "：" in text:
                    text = text.split("：", 1)[1].strip()
                    if text in translations:
                        used[text] = translations[text]

    visit(data)
    if used:
        return {**data, "_analysis_translations": used}
    return data


def english_analysis_view(text: str, translations: dict[str, str]) -> str:
    """给英文素材摘要使用译文，保留解析所需的原始标签名。"""

    def english(value):
        value = value.strip()
        return translations.get(value, select_analysis(value, "en"))

    lines, body = [], []
    for line in text.splitlines():
        match = re.match(r"^(动作|场景|人物|对象|人物情绪|氛围|景别|角度|镜头运动)：(.*)$", line)
        if match:
            lines.append(match[1] + "：" + "；".join(english(v) for v in match[2].split("；")))
        else:
            body.append(line)
    return "\n".join([english("\n".join(body)), *lines])
