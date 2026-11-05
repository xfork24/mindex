"""任务冻结配置及索引提交参数；队列提交与上层执行器共用。"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from mindex.shared.config import Config, StrictModel, absolutize_config_paths, relativize_config_paths
from mindex.shared.storage import dumps

BASE_ROLES = ("asr", "embedding", "analyzer", "person")
STUDIO_ROLES = ("generator", "writer", "tts", "video_generator", "image_generator", "music_generator", "animator")


def freeze_config(config, store, *, studio=False):
    # Python mode preserves integers used by historical index fingerprints.
    snapshot = config.model_dump()
    snapshot["data_dir"] = str(config.data_dir)
    # 数据目录内部的路径相对 data_dir.parent 存储，使目录移动/改名后仍可解析；
    # 外部路径（model_root、外部清单）保持绝对，由 restore_config 回读时还原。
    snapshot = relativize_config_paths(config.data_dir, snapshot)
    snapshot["_credential_profiles"] = {
        role: row["value"] if row else None
        for role in (*BASE_ROLES, *STUDIO_ROLES) if studio or role in BASE_ROLES
        for row in [store.one("SELECT value FROM settings WHERE key=?", ("active.profile." + role,))]
    }
    if studio and snapshot["_credential_profiles"].get("writer") is None:
        writer, generator = config.studio.writer, config.studio.generator
        if (writer.base_url, writer.model, writer.api_key) == (generator.base_url, generator.model, generator.api_key):
            snapshot["_credential_profiles"]["writer"] = snapshot["_credential_profiles"].get("generator")
    return dumps(snapshot)


def restore_config(raw, runtime, store):
    snapshot = json.loads(raw)
    profiles = snapshot.pop("_credential_profiles", {})
    # 快照里的可移植路径按当前 data_dir 还原为绝对路径（镜像 load_config 的 rebase），
    # 使历史任务在数据目录移动/改名后仍能解析 data_dir / 部署清单 / 字幕字体。
    snapshot = absolutize_config_paths(runtime.base.data_dir, snapshot)
    config = Config.model_validate(snapshot)
    preserve_numbers(config, snapshot)
    # 部署后端不进入冻结快照；恢复任务必须使用当前节点的配置和目录。
    for key in ("database", "vector", "storage", "queue", "locks", "gpu", "server", "uploads", "data_dir"):
        setattr(config, key, getattr(runtime.base, key))
    for role in (*BASE_ROLES, *STUDIO_ROLES):
        reference = profiles.get(role)
        profile = store.one("SELECT api_key,role,config FROM model_profiles WHERE id=?", (reference,)) if reference else None
        if reference and (profile is None or profile["role"] not in ({role, "generator"} if role == "writer" else {role})):
            raise ValueError(f"任务引用的 {role} 模型凭据已不存在或归属不符，请核对后重新提交")
        target = getattr(config if role in BASE_ROLES else config.studio, role)
        node = getattr(runtime.base if role in BASE_ROLES else runtime.base.studio, role)
        if profile:
            routing = json.loads(profile["config"])
            if any(key in routing and routing[key] != getattr(target, key) for key in ("base_url", "model")):
                raise ValueError(f"任务引用的 {role} 模型地址或身份已变化，请重新提交")
        target.api_key = profile["api_key"] or "" if profile else (
            node.api_key if (node.base_url, node.model) == (target.base_url, target.model) else "")
    return config


def preserve_numbers(model, snapshot):
    # 历史指纹区分 4 与 4.0；校验后的浮点转换不能改变冻结配置身份。
    for key, value in snapshot.items():
        current = getattr(model, key, None)
        if isinstance(value, dict) and isinstance(current, StrictModel):
            preserve_numbers(current, value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            setattr(model, key, value)


class IndexOptions(StrictModel):
    action: Literal["build", "activate", "lexical", "person-publish", "activate-release"] = "build"
    index_id: str | None = None
    release_id: str | None = None
    activate: bool = False
    mode: Literal["auto", "visual", "enriched"] = "auto"
    source_id: str | None = None
    kinds: list[Literal["moment", "shot", "scene"]] | None = Field(default=None, min_length=1)
    channels: list[Literal["visual", "dialogue", "analysis"]] | None = Field(default=None, min_length=1)
    replace: bool = False
