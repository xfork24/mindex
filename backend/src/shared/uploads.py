"""公共上传 helper：流式落盘并限制大小，供检索 API 与创作查询文件共用。"""

from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool


async def save_upload(upload: UploadFile, path: Path, max_bytes: int):
    """把上传流式写入 ``path``，超过 ``max_bytes`` 返回 413，空文件返回 422。

    任何失败（413、422、读写异常或请求取消）都会先删除已写入的半成品再重新抛出。
    """
    from shared.work_budget import GrowingReservation

    try:
        size = 0
        handle = await run_in_threadpool(path.open, "wb")
        with handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, "上传文件过大")
                with GrowingReservation(path) as reservation:
                    await run_in_threadpool(reservation.grow, len(chunk))
                    await run_in_threadpool(handle.write, chunk)
                    await run_in_threadpool(handle.flush)
        if not size:
            raise HTTPException(422, "上传文件为空")
    except BaseException:
        # 取消路径上不能再 await（可能被再次取消而留下半成品）；单次 unlink 开销可忽略。
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)  # noqa: ASYNC240 — 见上
        raise
    finally:
        await upload.close()
