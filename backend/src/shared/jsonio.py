"""文件原子写入的唯一实现（JSON、文本与字节）。

写入同目录的唯一临时文件（``<名称>.<随机>.partial``），fsync 后 ``os.replace`` 到目标：
读取方不会看到写了一半的文件，并发写同一目标也不会共用临时文件。临时名以 ``.partial`` 结尾，
崩溃残留可由 ``mindex clean`` 的既有规则识别。文本一律按 UTF-8 编码。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path


def save_bytes(path: Path, payload: bytes) -> None:
    """原子写入字节内容；失败时删除临时文件并重新抛出原异常。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".partial")
    temporary = Path(name)
    from mindex.shared.work_budget import GrowingReservation, current

    budget = current()
    if budget is not None:
        budget.retain(path)
    reservation = GrowingReservation(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            reservation.grow(len(payload))
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    finally:
        reservation.close()


def save_text(path: Path, text: str) -> None:
    """原子写入 UTF-8 文本。"""
    save_bytes(path, text.encode("utf-8"))


def save(path: Path, value) -> None:
    """原子写入 JSON（保留中文、两格缩进）。"""
    save_text(path, json.dumps(value, ensure_ascii=False, indent=2))
