"""ComfyUI 工作流与节点映射的纯校验（无网络）：配置加载、设置页保存与视频生成后端共用。

工作流必须是 ComfyUI「导出（API）」格式：``{节点ID: {"class_type": ..., "inputs": {...}}}``。
映射把视频生成请求的字段写入工作流节点输入，取值形如 ``"<节点ID>.inputs.<输入名>"``。
"""

from __future__ import annotations

import hashlib
import json

# 可映射的请求字段 → 中文名；prompt 必填，其余按工作流需要选填。
MAPPING_FIELDS = {
    "prompt": "正向提示词",
    "negative": "反向提示词",
    "seed": "随机种子",
    "frames": "帧数",
    "width": "宽度",
    "height": "高度",
    "image": "参考图片",
}
# 图片生成（aigc_image 分镜）与背景音乐生成的 ComfyUI 映射字段。
IMAGE_MAPPING_FIELDS = {
    "prompt": "正向提示词",
    "negative": "反向提示词",
    "seed": "随机种子",
    "width": "宽度",
    "height": "高度",
    "image": "主体参考图",
}
MUSIC_MAPPING_FIELDS = {
    "prompt": "音乐描述",
    "lyrics": "歌词",
    "seconds": "时长（秒）",
    "seed": "随机种子",
    "audio": "参考音频（翻唱）",
}


def parse_target(target: str) -> tuple[str, str]:
    """``"12.inputs.text"`` → ``("12", "text")``；格式错误抛 ValueError。"""
    parts = target.split(".") if isinstance(target, str) else []
    if len(parts) != 3 or parts[1] != "inputs" or not parts[0] or not parts[2]:
        raise ValueError(f"格式应为「节点ID.inputs.输入名」，例如 6.inputs.text，实际为 {target!r}")
    return parts[0], parts[2]


def validate_workflow(workflow: object, mapping: object, fields: dict[str, str] | None = None) -> None:
    """校验工作流为 API 格式、映射字段合法且每个映射目标都存在；错误为中文 ValueError。"""
    if not isinstance(workflow, dict) or not workflow:
        raise ValueError("ComfyUI 工作流必须是非空 JSON 对象")
    if "nodes" in workflow and "links" in workflow:
        raise ValueError(
            "这是 ComfyUI 界面格式的工作流；请在 ComfyUI 中用「导出（API）」（Export (API)）重新导出后再导入"
        )
    for node_id, node in workflow.items():
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("class_type"), str)
            or not isinstance(node.get("inputs"), dict)
        ):
            raise ValueError(
                f"工作流节点 {node_id} 不是 API 格式（需要 class_type 与 inputs）；"
                "请在 ComfyUI 中用「导出（API）」重新导出"
            )
    fields = fields or MAPPING_FIELDS
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("ComfyUI 节点映射不能为空，至少需要映射 prompt（正向提示词）")
    unknown = sorted(set(mapping) - set(fields))
    if unknown:
        raise ValueError(f"节点映射包含未知字段 {'、'.join(unknown)}；可选：{'、'.join(fields)}")
    if "prompt" not in mapping:
        raise ValueError("节点映射缺少 prompt（正向提示词）")
    for field, target in mapping.items():
        label = f"映射 {field}（{fields[field]}）"
        try:
            node_id, name = parse_target(target)
        except ValueError as exc:
            raise ValueError(f"{label}：{exc}") from None
        node = workflow.get(node_id)
        if node is None:
            raise ValueError(f"{label} → {target}：工作流中没有节点 {node_id}")
        if name not in node["inputs"]:
            raise ValueError(
                f"{label} → {target}：节点 {node_id}（{node['class_type']}）没有输入 {name}；"
                f"可用输入：{'、'.join(node['inputs']) or '无'}"
            )


def workflow_hash(workflow: dict | None) -> str | None:
    """工作流内容摘要（规范化 JSON 的 sha256 前 16 位），进入 generate 阶段签名。"""
    if workflow is None:
        return None
    raw = json.dumps(workflow, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
