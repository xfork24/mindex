"""集群协议与派生检索准入；维护入口仍可回填缺失投影。"""

from __future__ import annotations

from shared.errors import ModelError
from shared.lexical_store import lexical_ready

PROTOCOL = "2"


def validate_protocol(store):
    row = store.one("SELECT value FROM settings WHERE key='runtime.protocol'")
    if row is None or row["value"] != PROTOCOL:
        raise RuntimeError("素材库运行协议不匹配；请停止所有节点，执行 mindex migrate 并统一升级节点程序")


def validate_index(store, active):

    if active["spec"].get("format", 1) < 2:
        raise ModelError("集群不支持旧格式索引的全快照检索；请执行 mindex index --mode auto --activate 重建")
    if not lexical_ready(store, active["id"]):
        raise ModelError(
            f"集群索引缺少匹配分词器的派生数据；请执行 mindex indexes lexical --index-id {active['id']} "
            f"--replace"
        )


def validate_serving(context):
    if context.form != "cluster":
        return
    validate_protocol(context.store)
    active = context.store.active_index()
    if active is not None:
        validate_index(context.store, active)
        config = context.store.deployment_config
        if config is not None:
            space = active["spec"].get("space", {})
            for field in ("model", "dimensions", "quantization"):
                if field in space and space[field] != getattr(config.embedding, field):
                    raise RuntimeError(
                        f"集群活动索引的嵌入空间 {field} 与节点配置不一致；请统一模型配置或重新发布索引"
                    )
            preprocessing = active["spec"].get("preprocessing")
            if preprocessing is not None and preprocessing != config.video.model_dump():
                raise RuntimeError("集群活动索引的视频预处理与节点配置不一致；请统一配置或重新发布索引")
