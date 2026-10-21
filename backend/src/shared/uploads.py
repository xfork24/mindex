"""公共上传 helper：流式落盘并限制大小，供检索 API 与创作查询文件共用。"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, UploadFile


async def save_upload(upload: UploadFile, path: Path, max_bytes: int):
    try:
        size = 0
        with path.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, "上传文件过大")
                handle.write(chunk)
        if not size:
            raise HTTPException(422, "上传文件为空")
    finally:
        await upload.close()
