"""检查点的远端镜像与读取（plans §8 阶段 5）。

本地检查点（plans/transcripts/analysis/dialogue）继续作为节点快速路径；对象存储
形态下**写穿透**到 MediaStore（key 与相对路径一致），并在本地缺失时**回读**远端，
使新节点无需共享 data_dir 也能续跑。检查点是小文件，写穿透的额外 PUT 可接受；
权威副本在对象存储，节点目录可随时丢弃。
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from mindex.shared.jsonio import save_text as _write_local

log = logging.getLogger(__name__)

__all__ = ["key_of", "read_text", "save_text", "sync"]


def _remote(store):
    media = getattr(store, "media", None)
    if media is not None and getattr(media, "backend", "local") == "s3":
        return media
    return None


def key_of(store, path: Path) -> str:
    """绝对路径 → 相对数据目录的 POSIX key（与 SQLite 中的 path 列同约定）。"""
    return Path(path).resolve().relative_to(Path(store.root).resolve()).as_posix()


def save_text(store, path: Path, text: str) -> None:
    """写本地检查点并（S3 形态）镜像到对象存储。"""
    _write_local(Path(path), text)
    media = _remote(store)
    if media is None:
        return
    import hashlib

    key = key_of(store, path)
    try:
        media.put_immutable(key, io.BytesIO(text.encode("utf-8")),
                            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    except ValueError:  # 同名不同内容（检查点被重算）：以本地最新为准，覆盖远端
        media.delete_version(key)
        media.put_immutable(key, io.BytesIO(text.encode("utf-8")),
                            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


def sync(store, path: Path) -> None:
    """把已原地写好的本地检查点镜像到对象存储（atomic_json 等第三方写入之后调用）。"""
    media = _remote(store)
    if media is None or not Path(path).is_file():
        return
    save_text(store, Path(path), Path(path).read_text(encoding="utf-8"))


def read_text(store, path: Path) -> str | None:
    """读检查点：本地优先，缺失时从对象存储物化；都没有返回 None。"""
    path = Path(path)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    media = _remote(store)
    if media is None:
        return None
    try:
        with media.materialize(key_of(store, path)) as cached:
            return cached.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 — 远端不可达按缺失处理，由上层重算
        log.warning("读取远端检查点失败 %s：%s", path, exc)
        return None
