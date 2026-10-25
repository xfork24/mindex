"""阶段注册表：每个构建阶段一个 ``StageHandler``，集中阶段参数、签名载荷、依赖、预检、执行与冲突说明。

签名外壳（params → payload → fingerprint）仍在 ``signatures.specification``，处理器只提供
payload/inputs 与参数增补，保证与历史版本逐字节一致。具体处理器在 ``handlers/`` 包里，导入时
调用 ``register()`` 登记；登记顺序即阶段顺序（plan → shot → final），预检、冲突递归与 API
取值都按它展开。

每个分镜的阶段序列由镜头来源决定（``shot_stages``）：recall（缺省）走 ``STAGES``；上传片段 clip 没有召回，
只取配音、在片段内选窗与渲染；静态图片 image 只有配音与渲染（渲染按配音时长生成推拉画面）；AIGC 生成 aigc
先配音，再按配音时长生成画面（generate），然后在生成片段内选窗并渲染；AI 动画 animation 先配音，再由动画生成
LLM 写 HTML/JS 动画源码（animate），渲染阶段在沙箱里逐帧录制成画面（没有选片）；AI 生成图片 aigc_image 先配音，
再由图片生成模型生成图片（draw），之后按静态图片渲染。分镜带背景音乐提示点（生成/翻唱）时在配音后多一个
背景音乐阶段（music），成片合成时把各段背景音乐按分镜顺延铺在配音下面。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from mindex.studio import studio

from .versions import STAGES, version

Scope = Literal["plan", "shot", "final"]


@dataclass
class RunContext:
    """单次节点执行的上下文：``builder.execute`` 构造，传给 ``StageHandler.run``。"""

    config: Any
    job_id: str
    w: dict
    p: dict
    sid: str | None
    spec: dict
    directory: Any
    version_id: str
    diagnostics: Any
    # speaker/storyboarder/embedder/writer/video_generator/animator/animation_capture（可为 None）
    # 与 cancelled（构建取消检查，无参调用）
    services: dict = field(default_factory=dict)


class StageHandler:
    """阶段处理器基类。子类设置 ``name``/``label``/``scope`` 并覆盖需要的钩子。"""

    name: str = ""
    label: str = ""
    scope: Scope = "shot"
    reusable = True  # 构建时可按签名复用历史成功版本
    selects = True  # 执行成功后自动选用新版本（split 自行写入分镜工作台）
    uses_store = False  # settings 需要 Store（展示路径复用同一实例）
    checks_parameters = True  # 签名不符时，冲突说明是否附带“阶段参数不一致”
    choosable = True  # 可经 /versions/choose 选用
    buildable = True  # 可作为单节点重建（NodeBody.stage）
    column = True  # 可整列重生成（ColumnBody.stage）
    reason = "缺少匹配的成功版本"  # final 阶段预检节点的说明

    def pinned(self, item) -> bool:
        """当前选用的版本是否「钉住」：构建时直接复用、不按签名重建（例如人工修改的动画源码）。

        只有强制重建或单独执行该节点（force）才会替换钉住的版本。"""
        return False

    def settings(self, config, p, *, store=None, display=False) -> dict:
        raise NotImplementedError

    def specification(self, config, job_id, w, p, sid, params, resources):
        """返回 ``(payload, inputs, params)``；params 可在设置之上增补。"""
        raise NotImplementedError

    def dependencies(self, w, p, sid, *, resplit=False) -> list[str]:
        return []

    def prepare(self, config, w, p, sid) -> None:
        """执行前的方案修正（在计算签名之前调用）。"""

    def plan_node(self, config, job_id, w, p) -> dict | None:
        """plan 阶段：需要执行时返回预检节点，否则 None。"""
        return None

    def preflight(self, config, job_id, w, p, sid, spec, found, node) -> None:
        """shot 阶段：可把 ``node`` 的 action 改为 blocked/skip 并给出 reason。"""

    def column_nodes(self, config, job_id, w, p, inputs_by_shot) -> list[dict]:
        raise studio.StudioError("该阶段不支持列重生成")

    def column_reason(self, w, p, sid, spec) -> str | None:
        """列重生成时依赖都完好后的额外阻塞原因。"""
        return None

    def run(self, ctx: RunContext) -> tuple[dict, dict]:
        raise NotImplementedError

    def selected(self, p, sid):
        if self.scope == "final":
            return p.get("final")
        if self.scope == "plan":
            return p.get(self.name)
        return p["shots"].get(sid, {}).get(self.name)

    def pre_conflicts(self, w, p, sid, item) -> list[str]:
        """不论签名是否一致都要报告的冲突（例如锚点）。"""
        return []

    def conflict_labels(self, config, w, item, expected, messages) -> list[str]:
        """签名不一致时的阶段专属说明；``messages`` 是此前已收集的说明（只读）。"""
        return []


REGISTRY: dict[str, StageHandler] = {}


def register(handler: StageHandler) -> StageHandler:
    if not handler.name or not handler.label or handler.scope not in ("plan", "shot", "final"):
        raise ValueError(f"阶段处理器不完整：{handler!r}")
    REGISTRY[handler.name] = handler
    return handler


def handler(stage) -> StageHandler:
    found = REGISTRY.get(stage)
    if found is None:
        raise studio.StudioError("未知阶段")
    return found


def names(*, scope=None, flag=None) -> tuple[str, ...]:
    return tuple(
        name
        for name, item in REGISTRY.items()
        if (scope is None or item.scope == scope) and (flag is None or getattr(item, flag))
    )


# 镜头来源 → 逐镜阶段序列。来源缺省（旧分镜）等价 recall。
SHOT_SOURCES: dict[str, tuple[str, ...]] = {
    "recall": STAGES,
    "clip": ("tts", "select", "render"),
    "image": ("tts", "render"),
    "aigc_image": ("tts", "draw", "render"),
    "aigc": ("tts", "generate", "select", "render"),
    "animation": ("tts", "animate", "render"),
}
# 画面阶段：每个分镜至多一个（召回素材 / 生成图片 / 生成视频 / 生成动画），在分镜工作台里统一显示在“召回”列。
# 列操作用虚拟列名 picture 一次覆盖全部分镜各自的画面阶段（上传片段/图片的分镜没有画面阶段，不参与）。
PICTURE_STAGES = ("recall", "draw", "generate", "animate")
PICTURE_COLUMN = "picture"


def picture_stage(stage_list) -> str | None:
    """分镜阶段序列中的画面阶段；上传片段/图片来源没有，返回 None。"""
    return next((stage for stage in stage_list if stage in PICTURE_STAGES), None)


def shot_stages(content) -> tuple[str, ...]:
    """分镜内容（content 版本数据）对应的有序逐镜阶段。"""
    source = (content or {}).get("source") or {}
    kind = source.get("kind", "recall") if isinstance(source, dict) else "recall"
    stages = SHOT_SOURCES.get(kind)
    if stages is None:
        raise studio.StudioError(f"未知的镜头来源：{kind}")
    if music_cue(content):
        # 背景音乐提示点（生成/翻唱）在配音之后多一个背景音乐阶段；停止（none）与顺延的分镜没有。
        index = stages.index("tts") + 1
        stages = (*stages[:index], "music", *stages[index:])
    return stages


def music_cue(content) -> dict | None:
    """content 中需要生成背景音乐的提示点（kind 为 generate/cover）；没有或为停止时返回 None。"""
    cue = (content or {}).get("music")
    return cue if isinstance(cue, dict) and cue.get("kind") in ("generate", "cover") else None


def shot_content(w, p, sid):
    """逐镜阶段签名的公共部分：方案中分镜的指针与 content 版本数据。"""
    selected = p["shots"].get(sid)
    if selected is None:
        raise studio.StudioError("分镜不存在于当前方案")
    return selected, version(w, selected["content"])["data"]


def plan_shot_stages(w, p, sid) -> tuple[str, ...]:
    """方案中某分镜的阶段序列；分镜不在方案中时按 recall 来源处理。"""
    selected = p["shots"].get(sid)
    if not selected or selected.get("content") not in w["versions"]:
        return STAGES
    return shot_stages(version(w, selected["content"])["data"])
