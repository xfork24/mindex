"""阶段参数、版本签名与复用：settings/specification 决定签名，conflicts/match 据此判断兼容与复用。

各阶段的参数、载荷与冲突说明由 ``stages.REGISTRY`` 的处理器提供（``handlers/``）；这里只保留
外壳（参数 → 载荷 → 指纹），测试替换 ``_settings``、``specification``、``match``、``Store`` 均在本模块。

签名必须与历史版本逐字节一致（tests/test_studio_signature_snapshot.py 冻结）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.config import fingerprint, portable_parameters
from shared.shot_media import ShotMedia
from shared.storage import Store
from studio import studio
from studio.artifacts import digest

from .stages import handler, names, parse_final_key
from .versions import root, valid_files, version


@dataclass
class DetailContext:
    """Reuse reads within one detail response; never cache across edits or requests."""

    store: Store | None = None
    settings: dict = field(default_factory=dict)
    conflicts: dict = field(default_factory=dict)
    # 只读展示（detail）允许用 Store 的短 TTL 缓存读 active_index / visibility_key；
    # 构建/签名路径不走这里（另建 Store 实例），不受缓存影响。
    display: bool = False
    _media: object = None

    def database(self, config):
        if self.store is None:
            self.store = Store.open(config)
        return self.store

    def media(self, config):
        """Shared ``ShotMedia`` for one detail response. ``reuse_checks`` keeps probe
        results in memory across shots and reads the persisted probe cache without a
        per-file lock, so a job's shots that share source media are probed once."""
        if self._media is None:
            from shared.shot_media import ShotMedia

            self._media = ShotMedia(self.database(config), reuse_checks=True)
        return self._media


def settings(config, p, stage, *, context=None):
    if context is not None:
        if stage not in context.settings:
            context.settings[stage] = portable_parameters(
                config,
                _settings(
                    config,
                    p,
                    stage,
                    store=context.database(config) if handler(stage).uses_store else None,
                    display=context.display,
                ),
            )
        return context.settings[stage]
    return portable_parameters(config, _settings(config, p, stage))


def _settings(config, p, stage, *, store=None, display=False):
    item = handler(stage)
    if item.uses_store:
        store = store or Store.open(config)
    return item.settings(config, p, store=store, display=display)


class _Resources:
    """签名载荷所需的数据库与镜头媒体；详情请求内复用 DetailContext 的实例。"""

    def __init__(self, config, context):
        self.config, self.context = config, context

    def database(self):
        return self.context.database(self.config) if self.context is not None else Store.open(self.config)

    def media(self, store):
        return self.context.media(self.config) if self.context is not None else ShotMedia(store)


def recall_signature(recall):
    """规范化分镜召回设置；全默认返回 None，payload 与旧版逐字节一致以复用历史版本。"""
    if not recall:
        return None
    value = studio.ShotRecall.model_validate(recall) if isinstance(recall, dict) else recall
    filters = {
        k: v for k, v in value.filters.model_dump().items() if k != "source_ids" and v not in (None, 0, [])
    }
    if value.filters.source_group_ids:
        filters["source_group_ids"] = sorted(set(value.filters.source_group_ids))
    sources = sorted(value.filters.selected_sources())
    if len(sources) == 1:
        filters["source_id"] = sources[0]
    elif sources:
        filters.pop("source_id", None)
        filters["source_ids"] = sources
    signature = {
        "mode": value.mode,
        "person_ids": sorted(value.person_ids),
        "person_mode": value.person_mode,
        "person_relation": value.person_relation,
        "filters": filters,
    }
    if value.media_id:
        signature["media"] = {"id": value.media_id, "kind": value.media_kind}
    if (
        value.mode == "text"
        and not value.person_ids
        and value.person_mode == "auto"
        and value.person_relation == "all"
        and not filters
    ):
        return None
    return signature


def query_media(config, job_id, recall):
    """按内容寻址定位查询文件并复验 sha256；防止 inputs 被清理或篡改后用错文件。"""
    for path in sorted((root(config, job_id) / "inputs").glob(recall.media_id + ".*")):
        if path.suffix != ".partial" and digest(path) == recall.media_id:
            return path
    raise studio.StudioError("查询文件缺失或已损坏，请重新上传")


def specification(config, job_id, w, p, stage, sid=None, *, context=None, parameters=None):
    params = settings(config, p, stage, context=context)
    payload, inputs, params = handler(stage).specification(
        config, job_id, w, p, sid, params, _Resources(config, context)
    )
    if parameters is not None:
        params = parameters
    return {
        "inputs": inputs,
        "parameters": params,
        "signature": fingerprint({"stage": stage, "payload": payload, "parameters": params}),
    }


def comparable_parameters(config, parameters, expected):
    """Compare effective settings while preserving immutable historical hashes."""
    value = portable_parameters(config, parameters, expected)
    # llama_cpp.root() accepts both service roots and OpenAI /v1 URLs.
    # Restrict this equivalence to that adapter; other services may use /v1 differently.
    value = dict(value)
    embedding = value.get("embedding")
    if isinstance(embedding, dict) and embedding.get("backend") == "llama_cpp":
        embedding = dict(embedding)
        if isinstance(embedding.get("base_url"), str):
            embedding["base_url"] = embedding["base_url"].rstrip("/").removesuffix("/v1")
        value["embedding"] = embedding
    return value


def parameters_match(config, parameters, expected):
    return comparable_parameters(config, parameters, expected) == comparable_parameters(
        config, expected, expected
    )


PARAMETER_LABELS = {
    "search.channel_weights.visual": "召回权重（视觉）",
    "search.channel_weights.dialogue": "召回权重（对白）",
    "search.channel_weights.analysis": "召回权重（分析）",
    "visibility": "素材可见性指纹",
    "index_id": "活动索引版本",
    "person_release": "人物检索发布版本",
    "embedding.base_url": "嵌入服务地址",
    "embedding.model": "嵌入模型",
    "embedding.backend": "嵌入服务类型",
    "embedding.expected_space": "嵌入空间身份",
    "embedding.deployment_manifest": "嵌入部署清单",
    "limit": "召回数量",
    "sources": "任务素材范围",
    "source_groups": "素材组成员或状态",
    "scope_error": "素材组范围失效",
}


def parameter_conflicts(config, previous, current):
    """Report each effective setting difference, including identities and visibility."""
    messages = []

    def display(value):
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def walk(old, new, path=""):
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(old.keys() | new.keys()):
                walk(old.get(key), new.get(key), f"{path}.{key}" if path else key)
        elif old != new:
            label = PARAMETER_LABELS.get(path, path)
            messages.append(f"参数 {label}：原值 {display(old)}，当前 {display(new)}")

    walk(comparable_parameters(config, previous, current), comparable_parameters(config, current, current))
    return messages


def signature_matches(config, job_id, w, p, stage, sid, item, expected, *, context=None):
    if item["signature"] == expected["signature"]:
        return True
    if not parameters_match(config, item["parameters"], expected["parameters"]):
        return False
    # Reconstruct the current payload with the historical path spelling. This
    # verifies the original hash without rewriting immutable version records.
    legacy = specification(config, job_id, w, p, stage, sid, context=context, parameters=item["parameters"])
    return item["signature"] == legacy["signature"]


def conflicts(config, job_id, w, p, stage, sid, vid, *, context=None):
    key = (stage, sid, vid)
    if context is not None and key in context.conflicts:
        return context.conflicts[key]
    item = version(w, vid)
    stage_handler = handler(stage)
    expected = specification(config, job_id, w, p, stage, sid, context=context)
    messages = stage_handler.pre_conflicts(w, p, sid, item)
    pinned = stage_handler.selected(p, sid) == vid and stage_handler.pinned(item)
    if item["incomplete"]:
        messages.append("历史记录不完整，无法验证依赖与参数")
    # 钉住的版本（人工修改的动画源码）按设计跨签名复用，不报告自身的签名差异；上游冲突照常递归。
    if not pinned and not signature_matches(
        config, job_id, w, p, stage, sid, item, expected, context=context
    ):
        messages.extend(stage_handler.conflict_labels(config, w, item, expected, list(messages)))
        for key, value in expected["inputs"].items():
            if key != "content" and item["inputs"].get(key) != value:
                messages.append(
                    f"依赖 {key}：原版本 {item['inputs'].get(key) or '缺失'}，当前 {value or '缺失'}"
                )
        if stage_handler.checks_parameters and (
            not parameters_match(config, item["parameters"], expected["parameters"])
        ):
            messages.extend(parameter_conflicts(config, item["parameters"], expected["parameters"]))
        if not messages:
            messages.append("实际输入内容或素材文件已变化")
    # 上游成片阶段（导出、封面等以 ``阶段[:变体]`` 为依赖键）递归检查。
    finals = set(names(scope="final"))
    for dep_key, dep_id in expected["inputs"].items():
        dep_stage, dep_variant = parse_final_key(dep_key)
        if dep_stage in finals and dep_id and dep_id in w["versions"]:
            upstream = conflicts(config, job_id, w, p, dep_stage, dep_variant, dep_id, context=context)
            if upstream:
                messages.append(f"上游 {dep_key} 已变化：{upstream[0]}")
    # 上游逐镜阶段按注册顺序递归检查。
    for dependency in names(scope="shot"):
        dep_id = expected["inputs"].get(dependency)
        if dep_id and dep_id in w["versions"]:
            upstream = conflicts(config, job_id, w, p, dependency, sid, dep_id, context=context)
            if upstream:
                messages.append(f"上游 {dependency} 不兼容：{'；'.join(upstream)}")
    if context is not None:
        context.conflicts[key] = messages
    return messages


def match(config, job_id, w, p, stage, sid=None, *, verify=True):
    stage_handler = handler(stage)
    if not stage_handler.reusable:
        # Writing and re-splitting are explicit plan-level operations, never a pointer switch.
        return None
    selected = stage_handler.selected(p, sid)
    if selected and selected in w["versions"]:
        current = version(w, selected)
        if (
            stage_handler.pinned(current)
            and current["status"] == "complete"
            and not current["incomplete"]
            and (not verify or valid_files(config, job_id, current))
        ):
            return current["id"]
    spec = specification(config, job_id, w, p, stage, sid)
    items = list(reversed(list(w["versions"].values())))
    if selected:
        items.insert(0, version(w, selected))
    for item in items:
        if (
            item["stage"] == stage
            and item["shot_id"] == sid
            and item["status"] == "complete"
            and signature_matches(config, job_id, w, p, stage, sid, item, spec)
            and not item["incomplete"]
            and (not verify or valid_files(config, job_id, item))
        ):
            return item["id"]
    return None
