"""人物库的请求/实体模型与小型校验函数。"""

from __future__ import annotations

import math
import uuid
from contextlib import contextmanager
from typing import Literal

from pydantic import Field, model_validator

from shared.config import StrictModel


class Entity(StrictModel):
    """演员与作品角色是不同身份：演员跨作品共用；角色可归属一部或多部作品（跨电影角色）。"""

    kind: Literal["actor", "work", "role"]
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    work_ids: list[str] = Field(default_factory=list, max_length=50)
    actor_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def clean(self):
        self.name = self.name.strip()
        self.aliases = list(dict.fromkeys(x.strip() for x in self.aliases if x.strip()))
        if not self.name:
            raise ValueError("姓名不能为空")
        if self.kind == "role":
            self.work_ids = list(dict.fromkeys(self.work_ids))
            if not self.work_ids:
                raise ValueError("角色至少归属一部作品；跨电影角色可关联多部作品")
        else:
            self.work_ids = []
        return self


class CastEntry(StrictModel):
    """Cast rows carry identity only; staged characters are roles named like 老年叶淑柔."""

    image_id: str
    actor_name: str = Field(default="", max_length=200)
    foreign_name: str = ""
    role_name: str = ""
    actor_id: str | None = None
    role_id: str | None = None
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    # User must explicitly approve a single-person crop. Group photos are never accepted by guessing.
    single_person: bool = False

    @model_validator(mode="after")
    def identity_present(self):
        self.actor_name = self.actor_name.strip()
        self.foreign_name = self.foreign_name.strip()
        self.role_name = self.role_name.strip()
        if not self.actor_name and not self.actor_id:
            raise ValueError("请填写演员名或关联已有演员")
        return self


class CastConfirm(StrictModel):
    work_id: str
    entries: list[CastEntry]


class Review(StrictModel):
    """Internal per-observation correction used by cache validation and integrations."""

    actor_id: str | None = None
    role_id: str | None = None
    accept: bool = True
    reason: str = Field(min_length=1, max_length=1000)


class ShotSelection(StrictModel):
    """勾选一个出镜身份：演员 + 该作品中的角色。"""

    actor_id: str
    role_id: str | None = None


class ShotReview(StrictModel):
    source_id: str
    shot_id: str
    selections: list[ShotSelection] = Field(default_factory=list, max_length=50)


def uid():
    return uuid.uuid4().hex


def checked_box(box, width, height):
    if len(box) != 4:
        raise ValueError("照片区域必须有四个原图坐标")
    x1, y1, x2, y2 = box
    if not all(math.isfinite(x) for x in box) or not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise ValueError("照片区域超出原图或坐标无效")
    result = tuple(round(x) for x in box)
    if result[2] <= result[0] or result[3] <= result[1]:
        raise ValueError("照片区域太小")
    return result


class PersonHasLinkedShots(ValueError):
    def __init__(self, result):
        self.linked_shots = result
        examples = "；".join(
            f"{s['source_name']} {s['start']:.3f}–{s['end']:.3f} 秒（分镜 {s['shot_id']}）"
            for s in result["items"][:5]
        )
        super().__init__(f"该身份仍有关联出镜证据：{result['total']} 个未排除分镜；请先核对。{examples}")


@contextmanager
def frame_review_lock(store):
    from filelock import Timeout

    from shared import lock_backend

    try:
        with lock_backend.named(store, "pipeline"):
            yield
    except Timeout:
        raise ValueError("人物帧正在迁移或清理，请稍后重试审核") from None
