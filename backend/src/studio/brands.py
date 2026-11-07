"""品牌模板：内置模板（``studio/brands/*.yaml``，只读）与用户模板（metadata 库 ``brand_templates`` 表）。

一个品牌模板统一一类成片的风格：字幕样式、文字层样式、片头、片尾与水印。方案以 ``options.brand_id`` 选用，
再用 ``options.brand_overrides`` 覆盖其中几项（例如换一个片尾，或关掉水印）。

- 品牌素材（片头片尾视频、水印图片）按内容寻址存放在 ``data_dir/brand/<sha256><后缀>``，读取时复验摘要；
  模板里记录相对路径（``brand/…``），备份按数据库引用自动带上这些文件，Web 与桌面版共用同一份数据目录布局。
- ``content_hash`` 只覆盖影响成片的字段；名称、说明的修改不会让成片过期。
- 用户模板只能由复制产生（ID 以 ``user-`` 开头）；被任一方案引用时拒绝删除。
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import Field, ValidationError, model_validator

from admin.curation import EntityNotFound
from shared import media
from shared.config import StrictModel, fingerprint
from shared.images import image_png
from studio import studio
from studio.post_styles import OverlayStyle, SubtitleStyle

BUILTIN_DIR = Path(__file__).resolve().parent / "brands"
BUILTIN_ORDER = ("plain", "news", "tech")
USER_PREFIX = "user-"
BRAND_ID = r"^[a-z0-9][a-z0-9-]{0,63}$"
ASSET_DIR = "brand"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv")
MAX_ASSET_MB = 300
MAX_BOOKEND_SECONDS = 30
BOOKENDS = ("intro", "outro", "watermark")


class BrandReadOnly(ValueError):
    """内置品牌模板只读（API 返回 403）。"""


class BrandInUse(ValueError):
    """用户品牌模板被方案引用（API 返回 409）。"""


class AssetRef(StrictModel):
    """品牌素材引用：内容摘要、后缀与类型；文件在 ``data_dir/brand/<sha256><suffix>``。"""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    suffix: Literal[".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".mkv"]
    kind: Literal["image", "video"]
    name: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _kind_matches(self):
        if (self.kind == "image") != (self.suffix in IMAGE_SUFFIXES):
            raise ValueError("素材类型与文件后缀不一致")
        return self

    @property
    def path(self) -> str:
        return f"{ASSET_DIR}/{self.sha256}{self.suffix}"


class BrandClip(StrictModel):
    """片头或片尾：一段视频，按方案画幅与帧率适配（pad/crop 跟随方案）；``volume`` 为片头片尾原声音量。"""

    asset: AssetRef
    volume: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def _video(self):
        if self.asset.kind != "video":
            raise ValueError("片头片尾需要视频素材")
        return self


class Watermark(StrictModel):
    """水印：图片按短边比例缩放（``scale``），放在画面一角，``margin`` 为到边缘的距离（短边比例）。"""

    asset: AssetRef
    position: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = "top-right"
    opacity: float = Field(default=0.8, ge=0.05, le=1)
    scale: float = Field(default=0.12, ge=0.02, le=0.5)
    margin: float = Field(default=0.03, ge=0, le=0.2)
    include_bookends: bool = False

    @model_validator(mode="after")
    def _image(self):
        if self.asset.kind != "image":
            raise ValueError("水印需要图片素材")
        return self


class BrandTemplate(StrictModel):
    id: str = Field(pattern=BRAND_ID)
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=200)
    version: int = Field(default=1, ge=1)
    subtitle: SubtitleStyle | None = None
    overlay: OverlayStyle | None = None
    intro: BrandClip | None = None
    outro: BrandClip | None = None
    watermark: Watermark | None = None


class BrandChanges(StrictModel):
    """修改用户品牌模板：只填需要修改的字段；subtitle/overlay/intro/outro/watermark 显式传 null 表示去掉。"""

    name: str | None = Field(default=None, min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=200)
    subtitle: SubtitleStyle | None = None
    overlay: OverlayStyle | None = None
    intro: BrandClip | None = None
    outro: BrandClip | None = None
    watermark: Watermark | None = None


class BrandOverrides(StrictModel):
    """方案对所选品牌模板的覆盖：给出的项替换模板中的同名项；``disable`` 关掉模板中的片头、片尾或水印。"""

    subtitle: SubtitleStyle | None = None
    overlay: OverlayStyle | None = None
    intro: BrandClip | None = None
    outro: BrandClip | None = None
    watermark: Watermark | None = None
    disable: list[Literal["intro", "outro", "watermark"]] = Field(default_factory=list, max_length=3)

    @property
    def empty(self) -> bool:
        return not self.disable and all(
            getattr(self, key) is None for key in ("subtitle", "overlay", "intro", "outro", "watermark")
        )


EFFECT_FIELDS = ("subtitle", "overlay", "intro", "outro", "watermark")


def _load(path: Path) -> BrandTemplate:
    try:
        template = BrandTemplate.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise RuntimeError(f"内置品牌模板 {path.name} 无效：{exc}") from exc
    if template.id != path.stem or template.id.startswith(USER_PREFIX):
        raise RuntimeError(f"内置品牌模板 {path.name} 的 id 必须与文件名一致且不以 {USER_PREFIX} 开头")
    if template.intro or template.outro or template.watermark:
        raise RuntimeError(f"内置品牌模板 {path.name} 不能引用素材文件")
    return template


@lru_cache(maxsize=1)
def builtins() -> dict[str, BrandTemplate]:
    found = {path.stem: _load(path) for path in sorted(BUILTIN_DIR.glob("*.yaml"))}
    if set(found) != set(BUILTIN_ORDER):
        raise RuntimeError(f"内置品牌模板不完整：{sorted(set(found) ^ set(BUILTIN_ORDER))}")
    return {key: found[key] for key in BUILTIN_ORDER}


def effect(template: BrandTemplate) -> dict:
    """影响成片的品牌内容（字幕样式、文字层样式、片头片尾、水印）。"""
    return {
        key: (value.model_dump() if (value := getattr(template, key)) is not None else None)
        for key in EFFECT_FIELDS
    }


def content_hash(template: BrandTemplate) -> str:
    return fingerprint(effect(template))


def _row_template(row: dict) -> BrandTemplate:
    return BrandTemplate.model_validate({"id": row["id"], "name": row["name"], **json.loads(row["body"])})


def _body(template: BrandTemplate) -> str:
    return json.dumps(template.model_dump(exclude={"id", "name"}), ensure_ascii=False)


def _user_row(store, brand_id: str) -> dict | None:
    return store.one("SELECT * FROM brand_templates WHERE id=?", (brand_id,))


def get_brand(store, brand_id: str) -> BrandTemplate:
    found = builtins().get(brand_id)
    if found is not None:
        return found
    row = _user_row(store, brand_id) if brand_id.startswith(USER_PREFIX) else None
    if row is None:
        raise EntityNotFound(f"找不到品牌模板：{brand_id}")
    return _row_template(row)


def exists(store, brand_id: str) -> bool:
    try:
        get_brand(store, brand_id)
    except EntityNotFound:
        return False
    return True


def _view(template: BrandTemplate, row: dict | None = None) -> dict:
    return {
        **template.model_dump(),
        "hash": content_hash(template),
        "builtin": row is None,
        "base_id": row["base_id"] if row else None,
        "created_at": row["created_at"] if row else None,
        "updated_at": row["updated_at"] if row else None,
    }


def brand_view(store, brand_id: str) -> dict:
    template = get_brand(store, brand_id)
    return _view(template, None if template.id in builtins() else _user_row(store, template.id))


def list_brands(store) -> list[dict]:
    items = [_view(template) for template in builtins().values()]
    for row in store.rows("SELECT * FROM brand_templates ORDER BY created_at, id"):
        items.append(_view(_row_template(row), row))
    return items


def copy_brand(store, from_id: str, name: str, *, now: str) -> dict:
    source = get_brand(store, from_id)
    row = _user_row(store, source.id)
    base_id = (row["base_id"] or source.id) if row else source.id
    template = BrandTemplate.model_validate(
        {**source.model_dump(), "id": USER_PREFIX + uuid4().hex[:12], "name": name, "version": 1}
    )
    store.execute(
        "INSERT INTO brand_templates(id,name,base_id,body,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (template.id, template.name, base_id, _body(template), now, now),
    )
    return brand_view(store, template.id)


def _require_user(store, brand_id: str) -> dict:
    if brand_id in builtins():
        raise BrandReadOnly("内置品牌模板只读，请先复制为自己的模板再修改")
    row = _user_row(store, brand_id) if brand_id.startswith(USER_PREFIX) else None
    if row is None:
        raise EntityNotFound(f"找不到品牌模板：{brand_id}")
    return row


def update_brand(config, store, brand_id: str, changes: BrandChanges, *, now: str) -> dict:
    row = _require_user(store, brand_id)
    current = _row_template(row)
    values = changes.model_dump(exclude_unset=True)
    template = BrandTemplate.model_validate({**current.model_dump(), **values})
    for key in ("intro", "outro", "watermark"):
        if (clip := getattr(template, key)) is not None:
            require_asset(config, clip.asset)
    if content_hash(template) != content_hash(current):
        template = template.model_copy(update={"version": current.version + 1})
    store.execute(
        "UPDATE brand_templates SET name=?,body=?,updated_at=? WHERE id=?",
        (template.name, _body(template), now, brand_id),
    )
    return brand_view(store, brand_id)


def references(config, brand_id: str) -> list[dict]:
    """引用某品牌模板的方案（扫描全部创作任务的 workspace.json，只读）。"""

    root = studio.studio_root(config)
    found = []
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*/workspace.json")):
        try:
            w = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for plan in (w.get("plans") or {}).values():
            if (plan.get("options") or {}).get("brand_id") == brand_id:
                found.append(
                    {"job_id": path.parent.name, "plan_id": plan.get("id"), "plan_name": plan.get("name")}
                )
    return found


def delete_brand(config, store, brand_id: str) -> None:
    """删除用户品牌模板；被方案引用时拒绝。调用方须持有 studio 修改锁。素材文件保留（可能被其他模板共用）。"""
    _require_user(store, brand_id)
    used = references(config, brand_id)
    if used:
        first = used[0]
        raise BrandInUse(
            f"品牌模板正被 {len(used)} 个方案引用（如任务 {first['job_id']} "
            f"的方案「{first['plan_name']}」），"
            "请先为这些方案换用其他品牌模板"
        )
    store.execute("DELETE FROM brand_templates WHERE id=?", (brand_id,))


# ---------------------------------------------------------------- 素材


def asset_path(config, asset: AssetRef | dict) -> Path:
    asset = AssetRef.model_validate(asset) if isinstance(asset, dict) else asset
    return Path(config.data_dir) / asset.path


def require_asset(config, asset: AssetRef | dict) -> Path:
    """定位品牌素材并复验 sha256；缺失或被改动时报错（预检阻塞、合成失败）。"""

    path = asset_path(config, asset)
    sha = asset["sha256"] if isinstance(asset, dict) else asset.sha256
    if not path.is_file() or digest_file(path) != sha:
        raise studio.StudioError(f"品牌素材缺失或已损坏：{path.name}，请重新上传")
    return path


def digest_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def store_asset(config, staged: Path, suffix: str, name: str | None) -> dict:
    """校验并按内容寻址保存一个品牌素材，返回 ``AssetRef`` 视图（含时长、尺寸）。"""

    suffix = suffix.lower()
    if suffix not in IMAGE_SUFFIXES + VIDEO_SUFFIXES:
        raise studio.StudioError("品牌素材只支持 PNG/JPEG/WebP 图片或 MP4/MOV/WebM/MKV 视频")
    kind = "image" if suffix in IMAGE_SUFFIXES else "video"
    info = {}
    try:
        if kind == "image":
            # 延迟导入：保留可选依赖探测或异常捕获时的降级行为。
            from PIL import Image

            image_png(staged, config.video.max_pixels)
            with Image.open(staged) as image:
                info = {"width": image.width, "height": image.height, "alpha": "A" in image.getbands()}
        else:
            found = media.probe(staged)
            if found["duration"] > MAX_BOOKEND_SECONDS + 0.05:
                raise ValueError(f"片头片尾不能超过 {MAX_BOOKEND_SECONDS} 秒")
            info = {
                "duration": found["duration"],
                "width": found.get("width"),
                "height": found.get("height"),
                "audio": bool(found.get("audio")),
            }
    except studio.StudioError:
        raise
    except Exception as exc:
        raise studio.StudioError(f"品牌素材无法解码：{exc}") from exc
    sha = digest_file(staged)
    ref = AssetRef(sha256=sha, suffix=suffix, kind=kind, name=(name or "")[:120] or None)
    target = asset_path(config, ref)
    if not target.is_file() or digest_file(target) != sha:
        target.parent.mkdir(parents=True, exist_ok=True)
        studio.save_bytes(target, staged.read_bytes())
    return {**ref.model_dump(), **info, "url": f"/api/studio/brand-assets/{sha}{suffix}"}


# ---------------------------------------------------------------- 方案生效


def resolve(store, options: dict) -> dict | None:
    """方案生效的品牌内容：所选模板经 ``brand_overrides`` 覆盖与关闭后的结果；未选品牌时为 None。

    返回 ``{"id", "hash", "subtitle", "overlay", "intro", "outro", "watermark"}``（各项为 dict 或 None）。"""
    brand_id = options.get("brand_id")
    overrides = BrandOverrides.model_validate(options.get("brand_overrides") or {})
    if not brand_id and overrides.empty:
        return None
    result = effect(get_brand(store, brand_id)) if brand_id else dict.fromkeys(EFFECT_FIELDS)
    for key in EFFECT_FIELDS:
        if (value := getattr(overrides, key)) is not None:
            result[key] = value.model_dump()
    for key in overrides.disable:
        result[key] = None
    return {"id": brand_id, "hash": fingerprint(result), **result}
