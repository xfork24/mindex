"""本地来源：素材库主题分布（每个已分析素材一条候选，便于"按素材找选题"）与已做视频（去重用）。

不产生任何网络请求。已做视频不作为候选条目，只经 ``made_videos`` 供去重打分使用。
"""

from __future__ import annotations

import logging
from typing import ClassVar

from pydantic import Field

from mindex.shared.config import StrictModel
from mindex.topics.sources.base import FetchContext, Item, item, register

log = logging.getLogger(__name__)


class LocalConfig(StrictModel):
    library: bool = Field(default=True, description="把素材库里各素材的主题作为候选条目")


def made_videos(config) -> list[dict]:
    """已有创作任务的标题与大纲（不触发任务迁移，只读 job.json）。"""
    from mindex.studio import studio

    root = studio.studio_root(config)
    found = []
    if not root.is_dir():
        return found
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        try:
            state = studio.load_job(config, directory.name)
        except Exception:  # 损坏或旧格式的任务不影响选题
            log.warning("跳过无法读取的创作任务目录 %s", directory.name, exc_info=True)
            continue
        text = (state.get("outline") or state.get("input_text") or "").strip()
        found.append({"id": state["id"], "title": state.get("title") or "", "text": text[:600]})
    return found


class LocalSource:
    kind: ClassVar[str] = "local"
    label: ClassVar[str] = "本地素材库"
    config_model: ClassVar[type] = LocalConfig
    third_party: ClassVar[bool] = False
    secret: ClassVar[str | None] = None

    def fetch(self, ctx: FetchContext, cfg: LocalConfig) -> list[Item]:
        if not cfg.library:
            return []
        from mindex.studio.material_digest import build_digest
        from mindex.topics.profiles import material_state

        digest = build_digest(ctx.config, ctx.store, material_state(ctx.profile))
        items = []
        for source in digest.sources:
            summary = "；".join(filter(None, [
                "画面词：" + "、".join(source.vocabulary[:20]) if source.vocabulary else "",
                "故事线：" + "；".join(source.storyline[:6]) if source.storyline else "",
            ]))
            found = item(f"素材库：{source.name}", summary, url=None,
                         extra={"dedup": f"local:source:{source.source_id}", "source_id": source.source_id})
            if found:
                items.append(found)
        return items[: ctx.config.topics.fetch.max_items_per_source]


register(LocalSource())
