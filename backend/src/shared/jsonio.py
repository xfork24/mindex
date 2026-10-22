"""小型 JSON 文件原子写入。"""

from __future__ import annotations

import json
from pathlib import Path


def save(path: Path, value) -> None:
    """先写 `.partial` 再替换，读取方不会看到写了一半的文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)
