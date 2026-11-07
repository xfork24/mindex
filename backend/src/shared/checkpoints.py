"""检查点经不可变对象与数据库指针发布，本地模式保留原子文件语义。"""

from pathlib import Path

from shared import files

__all__ = ["key_of", "read_text", "save_text", "sync"]

key_of = files.key_for


def save_text(store, path: Path, text: str) -> None:
    files.save_bytes(store, path, text.encode("utf-8"))


def sync(store, path: Path) -> None:
    if Path(path).is_file():
        files.publish(store, path)


def read_text(store, path: Path) -> str | None:
    payload = files.read_bytes(store, path)
    return payload.decode("utf-8") if payload is not None else None
