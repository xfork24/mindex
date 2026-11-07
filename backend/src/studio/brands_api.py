"""品牌模板 API：``/api/studio/brands`` 与品牌素材 ``/api/studio/brand-assets``。

内置模板只读；用户模板由复制产生，可修改、删除（被方案引用时 409）。品牌素材（片头片尾视频、水印图片）
按内容寻址保存到 ``data_dir/brand/``，上传即校验可解码与时长上限。挂在创作路由上，删除与方案修改共用 studio
修改锁。
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import Field

from shared.config import StrictModel
from shared.uploads import save_upload
from studio import brands, studio
from studio.brands import BrandChanges, BrandInUse, BrandReadOnly

ASSET_NAME = re.compile(r"^([0-9a-f]{64})(\.[a-z0-9]{2,5})$")


class CopyBrandBody(StrictModel):
    name: str = Field(min_length=1, max_length=40)


def brand_routes(router, base, store, mutation_lock, *, current):
    """current() 返回最新配置。"""

    @router.get("/brands")
    def list_brands():
        return {"items": brands.list_brands(store)}

    @router.get("/brands/{brand_id}")
    def get_brand(brand_id: str):
        return brands.brand_view(store, brand_id)

    @router.post("/brands/{brand_id}/copy", status_code=201)
    def copy_brand(brand_id: str, body: CopyBrandBody):
        if not body.name.strip():
            raise studio.StudioError("模板名称不能为空")
        return brands.copy_brand(store, brand_id, body.name.strip(), now=studio.now())

    @router.put("/brands/{brand_id}")
    def update_brand(brand_id: str, body: BrandChanges):
        try:
            return brands.update_brand(base, store, brand_id, body, now=studio.now())
        except BrandReadOnly as exc:
            raise HTTPException(403, str(exc)) from exc

    @router.delete("/brands/{brand_id}")
    def delete_brand(brand_id: str):
        with mutation_lock():
            try:
                brands.delete_brand(base, store, brand_id)
            except BrandReadOnly as exc:
                raise HTTPException(403, str(exc)) from exc
            except BrandInUse as exc:
                raise HTTPException(409, str(exc)) from exc
        return {"deleted": brand_id}

    @router.post("/brand-assets", status_code=201)
    async def upload_asset(file: Annotated[UploadFile, File()]):
        """上传品牌素材：图片（水印，建议带透明通道的 PNG）或视频（片头片尾，不超过 30 秒）。"""
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in brands.IMAGE_SUFFIXES + brands.VIDEO_SUFFIXES:
            raise HTTPException(422, "品牌素材只支持 PNG/JPEG/WebP 图片或 MP4/MOV/WebM/MKV 视频")
        with tempfile.TemporaryDirectory(prefix="studio-brand-") as directory:
            staged = Path(directory) / f"asset{suffix}"
            await save_upload(file, staged, brands.MAX_ASSET_MB * 1024 * 1024)
            return await run_in_threadpool(brands.store_asset, current(), staged, suffix, file.filename)

    @router.get("/brand-assets/{name}")
    def get_asset(name: str, download: bool = False):
        match = ASSET_NAME.fullmatch(name)
        if not match or match.group(2) not in brands.IMAGE_SUFFIXES + brands.VIDEO_SUFFIXES:
            raise HTTPException(404, "找不到品牌素材")
        kind = "image" if match.group(2) in brands.IMAGE_SUFFIXES else "video"
        path = brands.require_asset(base, {"sha256": match.group(1), "suffix": match.group(2), "kind": kind})
        return FileResponse(
            path,
            filename=name if download else None,
            content_disposition_type="attachment" if download else "inline",
        )
