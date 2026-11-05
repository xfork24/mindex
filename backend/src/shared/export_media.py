"""便携 HTML 的媒体必须随导出复制，不能引用可淘汰的节点缓存。"""
from __future__ import annotations

import hashlib
from pathlib import Path

from mindex.shared.work_budget import copy_file


def copy_for_export(store, relative, directory):
    directory = Path(directory)
    name = hashlib.sha256(relative.encode()).hexdigest() + Path(relative).suffix
    destination = directory / "media" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    with store.materialize_media(relative) as source:
        copy_file(source, destination)
    return destination.relative_to(directory).as_posix()
