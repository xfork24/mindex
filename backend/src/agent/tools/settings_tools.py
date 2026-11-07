"""设置工具：模型档案、厂商预设、音色、业务调参。

- 读工具只返回掩码后的密钥（``has_key`` 与尾 4 位），与设置页一致。
- 新建或修改档案时，``api_key`` 只接受 ``secret://`` 引用（由 ask_user_question 的 secret 字段收集）或
  ``__clear__``；明文一律拒绝，防止密钥经模型往返。
- 自身档案保护：不允许修改当前智能助手正在使用的档案，也不允许把智能助手切换到其他档案
  （否则会话会在操作中途失去或改变自己的模型）。这类操作请在设置页完成。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from agent.tools.common import check
from shared.comfyui import (
    IMAGE_MAPPING_FIELDS,
    MAPPING_FIELDS,
    MUSIC_MAPPING_FIELDS,
    validate_workflow,
    workflow_hash,
)
from shared.config import StrictModel

ROLE_NAMES = Literal[
    "analyzer",
    "writer",
    "generator",
    "embedding",
    "asr",
    "tts",
    "image_generator",
    "video_generator",
    "music_generator",
    "animator",
    "person",
    "agent",
]
SETTINGS = {"domain": "settings"}
CLEAR_KEY = "__clear__"


async def load_settings(ctx: ToolContext) -> dict:
    return check(await ctx.client.get("/api/settings", probe_embedding="false"), "读取设置")


def find_profile(data: dict, profile_id: int) -> tuple[dict, dict]:
    for role in data["roles"]:
        for profile in role["profiles"]:
            if profile["id"] == profile_id:
                return role, profile
    raise ToolError(f"配置档案 {profile_id} 不存在", code="missing_profile")


def settings_focus(**extra) -> dict:
    return {"page": "settings", **{k: v for k, v in extra.items() if v is not None}}


def role_of(data: dict, profile_id: int) -> str | None:
    for role in data["roles"]:
        if any(p["id"] == profile_id for p in role["profiles"]):
            return role["role"]
    return None


def key_text(value: dict) -> str:
    return f"已设置（{value['hint']}）" if value.get("has_key") else "未设置"


def check_key(value: str | None) -> None:
    if value is None or value == CLEAR_KEY or value.startswith("secret://"):
        return
    raise ToolError(
        "api_key 必须是 ask_user_question 的 secret 字段返回的 secret:// 引用；不要让用户在对话里提供密钥",
        code="plaintext_secret",
    )


class Empty(StrictModel):
    pass


class GetSettings(StrictModel):
    role: ROLE_NAMES | None = Field(default=None, description="只看某个模型角色；省略返回全部")


async def get_settings(args: GetSettings, ctx: ToolContext) -> ToolOutput:
    data = await load_settings(ctx)
    roles = []
    lines = []
    for role in data["roles"]:
        if args.role and role["role"] != args.role:
            continue
        effective = role["effective"]
        item = {
            "role": role["role"],
            "label": role["label"],
            "active_profile_id": role["active_profile_id"],
            "effective": {
                k: effective.get(k)
                for k in ("backend", "base_url", "model", "max_tokens", "voice")
                if effective.get(k) is not None
            },
            "api_key": key_text(effective["api_key"]) if not effective.get("env_key_set") else "使用环境变量",
            "profiles": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "provider": p["provider"],
                    "model": p["config"].get("model"),
                    "base_url": p["config"].get("base_url"),
                    "api_key": key_text(p["api_key"]),
                    "active": p["id"] == role["active_profile_id"],
                }
                for p in role["profiles"]
            ],
        }
        roles.append(item)
        lines.append(
            f"{role['label']}({role['role']})：{effective['model']} · {effective['backend']} · "
            f"{effective['base_url']} · 密钥{item['api_key']}"
        )
        for profile in item["profiles"]:
            lines.append(
                f"  - 档案 #{profile['id']} {profile['name']} [{profile['provider']}] {profile['model']}"
                f"{' 【激活】' if profile['active'] else ''}"
            )
    value = {"roles": roles, "business": data["business"], "providers": data["providers"]}
    if not args.role:
        business = data["business"]
        lines.append(
            f"业务设置：GPU "
            f"互斥={business['enforce_gpu_lock']}，默认音色="
            f"{business['default_voice'] or '跟随语音合成档案'}，"
            f"检索三渠道权重={business['search']['channel_weights']}，rrf_k={business['search']['rrf_k']}，"
            f"候选数={business['search']['candidates']}"
        )
    focus = settings_focus(role=args.role) if args.role else settings_focus(section="models")
    return ToolOutput(value, text="\n".join(lines), focus=focus)


class Presets(StrictModel):
    role: ROLE_NAMES | None = Field(default=None, description="只看某个角色的预设")


async def list_provider_presets(args: Presets, ctx: ToolContext) -> ToolOutput:
    data = await load_settings(ctx)
    rows = []
    for provider, roles in data["presets"].items():
        for role, preset in roles.items():
            if args.role and role != args.role:
                continue
            rows.append(
                {
                    "provider": provider,
                    "provider_label": data["providers"].get(provider, provider),
                    "role": role,
                    "config": preset["config"],
                    "note": preset.get("note", ""),
                }
            )
    text = "\n".join(
        f"{r['provider']}({r['provider_label']}) · {r['role']}：{r['config']} {r['note']}" for r in rows
    )
    return ToolOutput(
        {"presets": rows, "providers": data["providers"]},
        text=text or "（没有匹配的预设）",
        focus=settings_focus(role=args.role, section=None if args.role else "models"),
    )


async def list_voices(args: Empty, ctx: ToolContext) -> ToolOutput:
    data = await load_settings(ctx)
    voices = [
        {
            "id": v["id"],
            "name": v["name"],
            "voice_id": v["voice_id"],
            "provider": v["provider"],
            "kind": v["kind"],
            "status": v["status"],
        }
        for v in data["voices"]
    ]
    business = data["business"]
    tts = next(r for r in data["roles"] if r["role"] == "tts")["effective"]
    text = (
        f"语音合成：{tts['model']}（{business['tts_backend']}），档案音色 {tts.get('voice')}；"
        f"默认音色覆盖：{business['default_voice'] or '无'}\n音色库：\n"
        + (
            "\n".join(
                f"#{v['id']} {v['name']} voice_id={v['voice_id']} [{v['provider']}/{v['kind']}] {v['status']}"
                for v in voices
            )
            or "（空）"
        )
    )
    return ToolOutput(
        {"voices": voices, "default_voice": business["default_voice"], "tts": tts},
        text=text,
        focus=settings_focus(section="voices"),
    )


class TestProfile(StrictModel):
    profile_id: int = Field(description="配置档案 ID")


async def test_model_profile(args: TestProfile, ctx: ToolContext) -> ToolOutput:
    role = role_of(await load_settings(ctx), args.profile_id)
    result = check(await ctx.client.post(f"/api/settings/profiles/{args.profile_id}/test"), "测试连接")
    text = (
        f"连接成功，耗时 {result['latency_ms']}ms" + (f"，{result['reply']}" if result.get("reply") else "")
        if result["ok"]
        else f"连接失败：{result.get('detail')}"
    )
    return ToolOutput(result, text=text, focus=settings_focus(role=role, profile_id=args.profile_id))


def mb(path: str | None) -> str:
    return path or "未定位"


class ProbeDeployment(StrictModel):
    profile_id: int = Field(description="llama_cpp 后端的嵌入/ASR/多模态理解配置档案 ID")


async def probe_model_deployment(args: ProbeDeployment, ctx: ToolContext) -> ToolOutput:
    role = role_of(await load_settings(ctx), args.profile_id)
    result = check(
        await ctx.client.post(f"/api/settings/profiles/{args.profile_id}/deployment/probe"), "e2e probe"
    )
    focus = settings_focus(role=role, profile_id=args.profile_id)
    if not result["ready"]:
        return ToolOutput(result, text=f"探测失败（服务未就绪）：{result['error']}", focus=focus)
    meta, caps, found = result.get("metadata") or {}, result.get("capabilities") or {}, result["detected"]
    lines = [
        f"服务就绪：{result['base_url']} · 模型 {result.get('model_id')} · 量化 {meta.get('ftype')} · "
        f"n_embd {meta.get('n_embd')} · 能力 {caps}",
        f"自动定位：weights={mb(found.get('weights_file'))}，mmproj={mb(found.get('mmproj_file'))}，"
        f"server_cwd={mb(found.get('server_cwd'))}",
    ]
    if result["current_manifest_valid"]:
        lines.append(f"档案已关联有效清单：{result['current_manifest']}")
    elif result["reusable_manifests"]:
        lines.append(
            f"可复用的一致清单：{', '.join(result['reusable_manifests'])}（pin_model_deployment 会直接关联）"
        )
    else:
        lines.append("没有与当前部署一致的清单；pin_model_deployment 会计算 SHA256 生成新清单")
    return ToolOutput(result, text="\n".join(lines), focus=focus)


class PinDeployment(StrictModel):
    profile_id: int = Field(description="llama_cpp 后端的嵌入/ASR/多模态理解配置档案 ID")
    quantization: str | None = Field(
        default=None, description="实际 GGUF 量化，例如 Q8_0；省略取档案或 GGUF 元数据"
    )
    weights_file: str | None = Field(
        default=None, description="本地 GGUF 路径；省略时从本机监听进程/props 自动定位"
    )
    mmproj_file: str | None = Field(
        default=None, description="本地 mmproj 路径；省略时从本机监听进程自动定位"
    )
    server_cwd: str | None = Field(default=None, description="llama-server 实际工作目录；省略时自动定位")
    weights_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="远程部署：操作员声明的 GGUF SHA256（与本地文件方式互斥）",
    )
    mmproj_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", description="远程部署：操作员声明的 mmproj SHA256"
    )
    launch_parameters: dict | None = Field(default=None, description="部署者确认的启动参数；参与部署身份")
    force_new: bool = Field(default=False, description="true=即使有一致的旧清单也生成新清单")


async def pin_target(args: PinDeployment, ctx: ToolContext) -> tuple[dict, dict]:
    role, profile = find_profile(await load_settings(ctx), args.profile_id)
    if role["role"] not in ("embedding", "asr", "analyzer"):
        raise ToolError("只有 llama_cpp 后端的嵌入、ASR、多模态理解档案需要部署清单", code="invalid_profile")
    return role, profile


async def preview_pin_deployment(args: PinDeployment, ctx: ToolContext) -> Preview:
    role, profile = await pin_target(args, ctx)
    remote = args.weights_sha256 is not None
    how = (
        "使用操作员声明的 SHA256"
        if remote
        else "使用指定文件计算 SHA256"
        if args.weights_file or args.mmproj_file
        else "优先复用与当前部署一致的旧清单，否则自动定位本机 GGUF/mmproj 并计算 SHA256（大文件约需数十秒）"
    )
    changes = [
        {
            "label": "部署清单",
            "before": profile["config"].get("deployment_manifest") or "未设置",
            "after": "新清单（data/deployments/，不覆盖旧文件）"
            if args.force_new
            else "一致的旧清单或新清单",
        },
        {"label": "方式", "after": how},
    ]
    warnings = (
        ["写入档案的 deployment_manifest，立即生效"] if role["active_profile_id"] == profile["id"] else []
    )
    if role["role"] == "embedding":
        warnings.append(
            "新的部署身份需要通过 e2e smoke 能力门槛后才能建索引/检索；复用旧清单时沿用已通过的门槛"
        )
    return Preview(
        f"为{role['label']}档案「{profile['name']}」执行 e2e pin",
        changes,
        warnings=warnings,
        focus=settings_focus(role=role["role"], profile_id=profile["id"]),
    )


async def pin_model_deployment(args: PinDeployment, ctx: ToolContext) -> ToolOutput:
    role, profile = await pin_target(args, ctx)
    body = args.model_dump(exclude={"profile_id"})
    result = check(
        await ctx.client.post(f"/api/settings/profiles/{args.profile_id}/deployment/pin", body), "e2e pin"
    )
    text = (
        f"{'已生成新清单' if result['created'] else '已复用一致的清单'} {result['manifest']}"
        f"（{result['trust']}，量化 {result['quantization']}，deployment {result['deployment_id']}），"
        f"已写入档案「{profile['name']}」。"
    )
    gate = result.get("gate")
    if gate is not None:
        text += (
            "smoke 能力门槛：已通过。"
            if gate["passed"]
            else f"smoke 能力门槛未通过：{gate.get('detail')}；需在命令行执行 mindex e2e phase smoke "
            f"后才能建索引/检索。"
        )
    text += "可用 test_model_profile 复测连接。"
    return ToolOutput(
        result,
        text=text,
        mutation=SETTINGS,
        focus=settings_focus(role=role["role"], profile_id=profile["id"]),
    )


class ProfileConfig(StrictModel):
    backend: str | None = Field(
        default=None, description="协议后端，例如 openai、anthropic、mindex、llama_cpp"
    )
    base_url: str | None = Field(default=None, description="服务 endpoint，例如 https://api.deepseek.com/v1")
    model: str | None = None
    revision: str | None = None
    timeout: float | None = Field(default=None, gt=0)
    api_key_env: str | None = Field(default=None, description="改用环境变量提供密钥时的变量名")
    llama_template: bool | None = Field(default=None, description="仅本地 llama-server；线上厂商须为 false")
    max_tokens: int | None = Field(default=None, gt=0)
    voice: str | None = Field(default=None, description="语音合成档案的默认音色")
    dimensions: int | None = Field(default=None, ge=1, description="嵌入维度")
    quantization: str | None = None
    deployment_manifest: str | None = Field(
        default=None,
        description="llama_cpp 嵌入/ASR/分析档案的部署清单路径（相对路径按数据目录解析）；"
        "通常由 pin_model_deployment 自动写入",
    )
    durations: list[float] | None = Field(
        default=None, min_length=1, description="视频生成：后端允许的生成时长（秒）"
    )
    poll_interval: float | None = Field(default=None, gt=0, description="视频生成：轮询间隔（秒）")
    max_wait: float | None = Field(default=None, gt=0, description="视频生成：单镜总等待上限（秒）")
    fps: int | None = Field(
        default=None, ge=1, le=60, description="视频生成（ComfyUI）：帧数映射 = 时长 × fps"
    )
    retries: int | None = Field(
        default=None, ge=0, le=10, description="动画生成：源码未通过静态校验时的重试次数"
    )
    max_capture_seconds: float | None = Field(
        default=None, gt=0, le=3600, description="动画生成：单个分镜逐帧录制的墙钟时长上限（秒）"
    )
    max_html_kb: int | None = Field(
        default=None, ge=1, le=5000, description="动画生成：动画源码大小上限（KiB）"
    )
    comfyui_workflow: dict | None = Field(
        default=None,
        description="视频生成（ComfyUI）：「导出（API）」格式工作流 JSON 对象 {节点ID: {class_type, inputs}}",
    )
    cover_model: str | None = Field(default=None, description="音乐生成：翻唱模型名（缺省 music-cover）")
    comfyui_mapping: dict[str, str] | None = Field(
        default=None,
        description='ComfyUI 视频/图片/音乐生成：请求字段 → "节点ID.inputs.输入名"；视频字段可选 '
        + "、".join(MAPPING_FIELDS)
        + "；图片字段可选 "
        + "、".join(IMAGE_MAPPING_FIELDS)
        + "；音乐字段可选 "
        + "、".join(MUSIC_MAPPING_FIELDS)
        + "；prompt 必填",
    )


COMFY_FIELDS = {
    "video_generator": MAPPING_FIELDS,
    "image_generator": IMAGE_MAPPING_FIELDS,
    "music_generator": MUSIC_MAPPING_FIELDS,
}


def check_video_config(role: str, config: dict) -> None:
    (
        """视频/图片/音乐生成档案的 ComfyUI """
        """工作流与映射先在本地校验，给出与设置页一致的中文错误（服务端仍会再校验）。"""
    )
    workflow, mapping = config.get("comfyui_workflow"), config.get("comfyui_mapping")
    if role not in COMFY_FIELDS or (workflow is None and mapping is None):
        return
    if workflow is None:
        raise ToolError("已填写 ComfyUI 节点映射，但没有导入工作流", code="invalid_profile")
    try:
        validate_workflow(workflow, mapping or {}, COMFY_FIELDS[role])
    except ValueError as exc:
        raise ToolError(str(exc), code="invalid_profile") from exc


def config_text(key: str, value):
    """预览里的字段值：工作流只显示节点数与摘要，不展开全文。"""
    if key == "comfyui_workflow" and isinstance(value, dict):
        return f"{len(value)} 个节点（摘要 {workflow_hash(value)}）"
    return value


class CreateProfile(StrictModel):
    role: ROLE_NAMES
    name: str = Field(min_length=1, max_length=60)
    provider: Literal["custom", "self", "zhipu", "deepseek", "aliyun", "tencent", "minimax", "anthropic"] = (
        "custom"
    )
    config: ProfileConfig
    api_key: str | None = Field(default=None, description="只能是 secret:// 引用")


async def preview_create_profile(args: CreateProfile, ctx: ToolContext) -> Preview:
    check_key(args.api_key)
    config = args.config.model_dump(exclude_none=True)
    check_video_config(args.role, config)
    changes = [{"label": key, "after": config_text(key, value)} for key, value in config.items()]
    changes.append({"label": "API Key", "after": "已通过表单提供" if args.api_key else "未提供"})
    warnings = (
        ["测试连接只做鉴权/可达性探测，不发起生成；激活后 aigc 分镜构建时会调用该服务，可能产生费用"]
        if args.role in ("video_generator", "image_generator", "music_generator")
        else []
    )
    return Preview(
        f"新建「{args.role}」角色的配置档案「{args.name}」（不会自动激活）",
        changes,
        warnings=warnings,
        focus=settings_focus(role=args.role),
    )


async def create_model_profile(args: CreateProfile, ctx: ToolContext) -> ToolOutput:
    check_key(args.api_key)
    body = {
        "role": args.role,
        "name": args.name,
        "provider": args.provider,
        "config": args.config.model_dump(exclude_none=True),
    }
    check_video_config(args.role, body["config"])
    if args.api_key:
        body["api_key"] = args.api_key  # 回环客户端在发送前解引用
    profile = check(await ctx.client.post("/api/settings/profiles", body), "新建配置档案")
    return ToolOutput(
        {"profile_id": profile["id"], "api_key": profile["api_key"]},
        text=f"已新建档案 #{profile['id']}「{profile['name']}」，密钥{key_text(profile['api_key'])}。"
        "可先 test_model_profile 测试连接，再 activate_model_profile 激活。",
        mutation=SETTINGS,
        focus=settings_focus(role=args.role, profile_id=profile["id"]),
    )


class UpdateProfile(StrictModel):
    profile_id: int
    name: str | None = Field(default=None, min_length=1, max_length=60)
    config: ProfileConfig | None = Field(default=None, description="只填需要修改的字段，其余保持不变")
    api_key: str | None = Field(default=None, description="secret:// 引用；__clear__ 清除；省略保持原密钥")


async def profile_body(args: UpdateProfile, ctx: ToolContext):
    check_key(args.api_key)
    if ctx.agent_profile_id is not None and args.profile_id == ctx.agent_profile_id:
        raise ToolError(
            "不能修改智能助手当前正在使用的档案（会影响本会话自身）；请在设置页修改", code="self_profile"
        )
    data = await load_settings(ctx)
    role, profile = find_profile(data, args.profile_id)
    config = {**profile["config"], **(args.config.model_dump(exclude_none=True) if args.config else {})}
    check_video_config(role["role"], config)
    body = {
        "role": role["role"],
        "name": args.name or profile["name"],
        "provider": profile["provider"],
        "config": config,
    }
    if args.api_key:
        body["api_key"] = args.api_key
    return role, profile, body


async def preview_update_profile(args: UpdateProfile, ctx: ToolContext) -> Preview:
    role, profile, body = await profile_body(args, ctx)
    changes = [
        {
            "label": key,
            "before": config_text(key, profile["config"].get(key)),
            "after": config_text(key, value),
        }
        for key, value in body["config"].items()
        if profile["config"].get(key) != value
    ]
    if body["name"] != profile["name"]:
        changes.append({"label": "名称", "before": profile["name"], "after": body["name"]})
    if args.api_key:
        changes.append(
            {
                "label": "API Key",
                "before": key_text(profile["api_key"]),
                "after": "清除" if args.api_key == CLEAR_KEY else "替换为表单提供的新密钥",
            }
        )
    warnings = ["该档案正在使用，修改立即生效"] if role["active_profile_id"] == profile["id"] else []
    return Preview(
        f"修改{role['label']}档案「{profile['name']}」",
        changes,
        warnings=warnings,
        focus=settings_focus(role=role["role"], profile_id=profile["id"]),
    )


async def update_model_profile(args: UpdateProfile, ctx: ToolContext) -> ToolOutput:
    role, _, body = await profile_body(args, ctx)
    profile = check(await ctx.client.put(f"/api/settings/profiles/{args.profile_id}", body), "修改配置档案")
    return ToolOutput(
        {"profile_id": profile["id"]},
        text=f"已修改档案 #{profile['id']}「{profile['name']}」。",
        mutation=SETTINGS,
        focus=settings_focus(role=role["role"], profile_id=profile["id"]),
    )


class ActivateProfile(StrictModel):
    profile_id: int


async def activation_target(args: ActivateProfile, ctx: ToolContext):
    data = await load_settings(ctx)
    role, profile = find_profile(data, args.profile_id)
    if (
        role["role"] == "agent"
        and ctx.agent_profile_id is not None
        and args.profile_id != ctx.agent_profile_id
    ):
        raise ToolError(
            "不能在会话中切换智能助手自身的模型档案（当前会话会失去原模型）；请在设置页切换",
            code="self_profile",
        )
    return role, profile


async def preview_activate_profile(args: ActivateProfile, ctx: ToolContext) -> Preview:
    role, profile = await activation_target(args, ctx)
    current = next(
        (p["name"] for p in role["profiles"] if p["id"] == role["active_profile_id"]), "YAML 基础配置"
    )
    warnings = ["保存即热生效：之后的任务与请求都会使用新模型"]
    if role["role"] == "embedding":
        warnings.append("切换嵌入模型后，若身份与已发布索引不一致，检索将不可用，直至重建并发布索引")
    if role["role"] in (
        "generator",
        "writer",
        "tts",
        "video_generator",
        "image_generator",
        "music_generator",
        "animator",
    ):
        warnings.append("已有创作产物不受影响；下次构建时受影响的节点会重建")
    if role["role"] == "animator":
        warnings.append(
            "激活即启用动画生成：之后构建 animation 分镜会调用该模型编写动画源码（人工修改的源码保留）"
        )
    if role["role"] == "video_generator":
        warnings.append("激活即启用视频生成：之后构建 aigc 分镜会调用该服务，可能产生费用")
    if role["role"] == "image_generator":
        warnings.append("激活即启用图片生成：之后构建 aigc_image 分镜会调用该服务，可能产生费用")
    if role["role"] == "music_generator":
        warnings.append("激活即启用音乐生成：之后构建带背景音乐提示点的分镜会调用该服务，可能产生费用")
    return Preview(
        f"激活{role['label']}档案",
        [{"label": role["label"], "before": current, "after": profile["name"]}],
        warnings=warnings,
        focus=settings_focus(role=role["role"], profile_id=profile["id"]),
    )


async def activate_model_profile(args: ActivateProfile, ctx: ToolContext) -> ToolOutput:
    role, profile = await activation_target(args, ctx)
    result = check(
        await ctx.client.post(f"/api/settings/profiles/{args.profile_id}/activate"), "激活配置档案"
    )
    text = f"已激活{role['label']}档案「{profile['name']}」。"
    if result.get("index_warning"):
        text += "警告：" + result["index_warning"]["message"]
    return ToolOutput(
        result,
        text=text,
        mutation=SETTINGS,
        focus=settings_focus(role=role["role"], profile_id=profile["id"]),
    )


class SearchTuning(StrictModel):
    channel_weights: dict[str, float] | None = Field(
        default=None, description="新版三渠道权重 visual/dialogue/analysis"
    )
    weights: dict[str, float] | None = Field(
        default=None, description="旧版五路权重 video/description/style/dialogue/lexical"
    )
    rrf_k: int | None = Field(default=None, ge=1)
    candidates: int | None = Field(default=None, ge=1, le=1000)


class Business(StrictModel):
    enforce_gpu_lock: bool | None = Field(default=None, description="GPU 互斥锁")
    search: SearchTuning | None = None
    default_voice: str | None = Field(default=None, description="默认音色 voice_id（覆盖语音合成档案的音色）")
    default_voice_id: int | None = Field(default=None, description="或用音色库记录 ID 指定默认音色")
    clear_default_voice: bool = Field(default=False, description="true=清除默认音色覆盖")


async def preview_business(args: Business, ctx: ToolContext) -> Preview:
    current = (await load_settings(ctx))["business"]
    changes = []
    if args.enforce_gpu_lock is not None:
        changes.append(
            {"label": "GPU 互斥", "before": current["enforce_gpu_lock"], "after": args.enforce_gpu_lock}
        )
    if args.search:
        for key, value in args.search.model_dump(exclude_none=True).items():
            changes.append({"label": f"检索 {key}", "before": current["search"].get(key), "after": value})
    if args.clear_default_voice:
        changes.append({"label": "默认音色", "before": current["default_voice"], "after": "清除"})
    elif args.default_voice or args.default_voice_id is not None:
        changes.append(
            {
                "label": "默认音色",
                "before": current["default_voice"],
                "after": args.default_voice or f"音色库 #{args.default_voice_id}",
            }
        )
    if not changes:
        raise ToolError("没有需要修改的业务设置", code="invalid_arguments")
    return Preview("修改业务设置（保存即热生效）", changes, focus=business_focus(args))


def business_focus(args: Business) -> dict:
    if args.clear_default_voice or args.default_voice or args.default_voice_id is not None:
        return settings_focus(section="voices")
    if args.search:
        return settings_focus(section="tuning")
    return settings_focus(section="processing")


async def update_business_settings(args: Business, ctx: ToolContext) -> ToolOutput:
    body = args.model_dump(exclude_none=True)
    if not body.get("clear_default_voice"):
        body.pop("clear_default_voice", None)
    result = check(await ctx.client.put("/api/settings/business", body), "修改业务设置")
    return ToolOutput(result, text=f"已更新业务设置：{result}", mutation=SETTINGS, focus=business_focus(args))


class DeleteProfile(StrictModel):
    profile_id: int = Field(description="要删除的配置档案 ID")


async def deletion_target(args: DeleteProfile, ctx: ToolContext):
    """删除前的保护：智能助手自身档案、各角色正在使用的档案都拒绝，且不发送删除请求。"""
    if ctx.agent_profile_id is not None and args.profile_id == ctx.agent_profile_id:
        raise ToolError("不能删除智能助手当前正在使用的档案；请在设置页处理", code="self_profile")
    role, profile = find_profile(await load_settings(ctx), args.profile_id)
    if role["active_profile_id"] == profile["id"]:
        raise ToolError(
            f"档案「{profile['name']}」是{role['label']}当前正在使用的档案，无法删除；请先激活其他档案",
            code="active_profile",
        )
    return role, profile


async def preview_delete_profile(args: DeleteProfile, ctx: ToolContext) -> Preview:
    role, profile = await deletion_target(args, ctx)
    return Preview(
        f"删除{role['label']}档案「{profile['name']}」",
        [
            {
                "label": "档案",
                "before": f"#{profile['id']} {profile['name']} [{profile['provider']}] "
                f"{profile['config'].get('model') or ''}".strip(),
                "after": "删除",
            }
        ],
        warnings=["删除后不可恢复，档案中保存的密钥一并删除"],
        focus=settings_focus(role=role["role"], profile_id=profile["id"]),
    )


async def delete_model_profile(args: DeleteProfile, ctx: ToolContext) -> ToolOutput:
    role, profile = await deletion_target(args, ctx)
    check(await ctx.client.delete(f"/api/settings/profiles/{args.profile_id}"), "删除配置档案")
    return ToolOutput(
        {"deleted": args.profile_id, "role": role["role"]},
        text=f"已删除{role['label']}档案 #{profile['id']}「{profile['name']}」。",
        mutation=SETTINGS,
        focus=settings_focus(role=role["role"]),
    )


async def preview_reset_business(args: Empty, ctx: ToolContext) -> Preview:
    business = (await load_settings(ctx))["business"]
    search = business["search"]
    changes = [
        {"label": "GPU 互斥", "before": business["enforce_gpu_lock"], "after": "恢复配置文件值"},
        {
            "label": "检索调参",
            "before": f"三渠道权重={search['channel_weights']}，rrf_k={search['rrf_k']}，"
            f"候选数={search['candidates']}",
            "after": "恢复配置文件值",
        },
        {
            "label": "默认音色",
            "before": business["default_voice"] or "无覆盖",
            "after": "清除覆盖（跟随语音合成档案）",
        },
    ]
    return Preview(
        "重置业务设置：删除页面上保存的全部业务覆盖，恢复配置文件中的值（保存即热生效）",
        changes,
        focus=settings_focus(section="processing"),
    )


async def reset_business_settings(args: Empty, ctx: ToolContext) -> ToolOutput:
    check(await ctx.client.delete("/api/settings/business"), "重置业务设置")
    business = (await load_settings(ctx))["business"]
    return ToolOutput(
        business,
        text=f"已重置业务设置，当前值：{business}",
        mutation=SETTINGS,
        focus=settings_focus(section="processing"),
    )


async def list_voice_catalog(args: Empty, ctx: ToolContext) -> ToolOutput:
    data = check(await ctx.client.get("/api/settings/voices/catalog"), "读取音色目录")
    clone = data.get("clone") or {}
    lines = [
        f"语音合成后端：{data['backend']}；默认音色覆盖：{data.get('default_voice') or '无'}；"
        f"克隆：{'支持（' + str(clone.get('mode')) + '）' if clone.get('supported') else '目录未声明'}"
    ]
    if data.get("error"):
        lines.append(f"目录查询出错：{data['error']}")
    for group in data.get("groups") or []:
        voices = group.get("voices") or []
        shown = "、".join(f"{v.get('label') or v['voice_id']}({v['voice_id']})" for v in voices[:40])
        more = f" 等 {len(voices)} 个" if len(voices) > 40 else ""
        lines.append(f"{group.get('label')}：{shown or '（空）'}{more}")
    library = data.get("library") or []
    lines.append(
        "音色库："
        + (
            "、".join(f"{v.get('label') or v.get('name') or v['voice_id']}({v['voice_id']})" for v in library)
            or "（空）"
        )
    )
    return ToolOutput(data, text="\n".join(lines), focus=settings_focus(section="voices"))


class RegisterVoice(StrictModel):
    name: str = Field(min_length=1, max_length=60, description="音色显示名")
    provider: Literal["minimax", "dashscope", "self", "openai"] = Field(description="音色所属厂商")
    voice_id: str = Field(min_length=1, max_length=200, description="厂商侧的音色 ID")
    kind: Literal["preset", "clone"] = "preset"
    notes: str = Field(default="", max_length=400)


async def preview_register_voice(args: RegisterVoice, ctx: ToolContext) -> Preview:
    changes = [{"label": k, "after": v} for k, v in args.model_dump().items() if v not in ("", None)]
    return Preview(f"把音色「{args.name}」登记到音色库", changes, focus=settings_focus(section="voices"))


async def register_voice(args: RegisterVoice, ctx: ToolContext) -> ToolOutput:
    voice = check(await ctx.client.post("/api/settings/voices", args.model_dump()), "登记音色")
    return ToolOutput(
        {"voice": voice},
        text=f"已登记音色 #{voice['id']}「{voice['name']}」voice_id={voice['voice_id']}。",
        mutation=SETTINGS,
        focus=settings_focus(section="voices"),
    )


class DeleteVoice(StrictModel):
    voice_id: int = Field(description="音色库记录 ID（list_voices 中的 #ID，不是厂商 voice_id）")


async def voice_target(args: DeleteVoice, ctx: ToolContext) -> tuple[dict, dict]:
    data = await load_settings(ctx)
    voice = next((v for v in data["voices"] if v["id"] == args.voice_id), None)
    if voice is None:
        raise ToolError(f"音色 #{args.voice_id} 不存在", code="missing_voice")
    return data, voice


async def preview_delete_voice(args: DeleteVoice, ctx: ToolContext) -> Preview:
    data, voice = await voice_target(args, ctx)
    warnings = ["只删除音色库记录，厂商侧的音色不会被删除"]
    if data["business"]["default_voice"] == voice["voice_id"]:
        warnings.append("这是当前默认音色：删除后默认音色覆盖会一并清除，改为跟随语音合成档案")
    return Preview(
        f"从音色库删除「{voice['name']}」",
        [
            {
                "label": "音色",
                "before": f"#{voice['id']} {voice['name']} voice_id={voice['voice_id']} "
                f"[{voice['provider']}/{voice['kind']}]",
                "after": "删除",
            }
        ],
        warnings=warnings,
        focus=settings_focus(section="voices"),
    )


async def delete_voice(args: DeleteVoice, ctx: ToolContext) -> ToolOutput:
    data, voice = await voice_target(args, ctx)
    check(await ctx.client.delete(f"/api/settings/voices/{args.voice_id}"), "删除音色")
    cleared = data["business"]["default_voice"] == voice["voice_id"]
    return ToolOutput(
        {"deleted": args.voice_id, "default_cleared": cleared},
        text=f"已删除音色 #{voice['id']}「{voice['name']}」"
        + ("，并清除了默认音色覆盖。" if cleared else "。"),
        mutation=SETTINGS,
        focus=settings_focus(section="voices"),
    )


CONSENT = "我确认拥有该声音的合法使用授权"
DECLINE = "取消"
CLONE_UPLOAD = {"openai", "minimax"}  # 上传样本克隆；dashscope 用公网 URL 复刻


class CloneVoice(StrictModel):
    """注意：没有 consent 字段——授权只能由用户在表单中确认，模型无法代为声明。"""

    name: str | None = Field(default=None, max_length=60, description="建议的音色名称（表单中可修改）")
    ref_text: str | None = Field(default=None, max_length=400, description="样本对应的参考文本（可选）")
    attachment: str | None = Field(
        default=None, description="声音样本附件 attachment://…（openai、minimax 后端必填；wav/mp3/m4a）"
    )
    audio_url: str | None = Field(
        default=None, max_length=1000, description="公网可访问的样本 URL（dashscope 后端）"
    )


async def clone_backend(args: CloneVoice, ctx: ToolContext) -> str:
    backend = (await load_settings(ctx))["business"]["tts_backend"]
    if backend in CLONE_UPLOAD:
        if not args.attachment:
            raise ToolError(
                f"当前语音合成后端 {backend} 需要上传声音样本：请让用户把 wav/mp3/m4a 样本作为附件发送，"
                "再传 attachment",
                code="invalid_arguments",
            )
    elif backend != "dashscope":
        raise ToolError(
            f"当前语音合成后端 {backend} 不支持克隆；请先激活 MiniMax（上传样本）或阿里云（URL 复刻）档案",
            code="unsupported_backend",
        )
    return backend


async def preview_clone_voice(args: CloneVoice, ctx: ToolContext) -> Preview:
    backend = await clone_backend(args, ctx)
    changes = [
        {"label": "语音合成后端", "after": backend},
        {
            "label": "样本",
            "after": args.attachment if backend in CLONE_UPLOAD else (args.audio_url or "在表单中填写 URL"),
        },
    ]
    if args.name:
        changes.append({"label": "音色名称", "after": args.name})
    return Preview(
        "克隆音色（确认后会弹出表单，由你本人确认声音授权）",
        changes,
        warnings=["克隆会把声音样本发送给语音合成服务；只有在表单中勾选授权确认后才会执行"],
        focus=settings_focus(section="voices"),
    )


def consent_question(args: CloneVoice, backend: str) -> dict:
    fields = [
        {
            "id": "name",
            "label": "音色名称",
            "type": "text",
            "required": True,
            **({"default": args.name} if args.name else {}),
        },
        {
            "id": "ref_text",
            "label": "参考文本（可选）",
            "type": "text",
            **({"default": args.ref_text} if args.ref_text else {}),
        },
    ]
    if backend == "dashscope":
        fields.append(
            {
                "id": "audio_url",
                "label": "样本公网 URL",
                "type": "text",
                "required": True,
                **({"default": args.audio_url} if args.audio_url else {}),
            }
        )
    return {
        "id": "clone_consent",
        "header": "声音授权",
        "fields": fields,
        "question": "克隆音色前请确认：你拥有该声音的合法使用授权（本人声音或已获得声音所有者授权）。",
        "options": [{"label": CONSENT}, {"label": DECLINE}],
    }


def consented(answer: dict) -> dict | None:
    """只有表单答复中明确选择了授权项才返回表单值；其余（取消、拒绝、未选）一律返回 None。"""
    if answer.get("cancelled"):
        return None
    item = next((a for a in answer.get("answers") or [] if a.get("id") == "clone_consent"), None)
    if item is None or CONSENT not in (item.get("selected") or []) or DECLINE in (item.get("selected") or []):
        return None
    return item.get("values") or {}


async def clone_voice(args: CloneVoice, ctx: ToolContext) -> ToolOutput:
    runtime = ctx.extra.get("runtime")
    if runtime is None or ctx.session_id is None:
        raise ToolError(
            "需要在 Mindex 页面助手中确认声音授权；当前环境无法弹出授权表单", code="consent_unavailable"
        )
    backend = await clone_backend(args, ctx)
    answer = await runtime.ask_question(
        ctx.session_id, ctx.extra.get("call_id"), [consent_question(args, backend)]
    )
    values = consented(answer)
    if values is None:
        raise ToolError("用户未确认声音授权，已取消克隆", code="consent_declined")
    name = str(values.get("name") or args.name or "").strip()
    if not name:
        raise ToolError("表单未填写音色名称，已取消克隆", code="invalid_arguments")
    data = {
        "name": name,
        "consent": True,
        "ref_text": str(values.get("ref_text") or args.ref_text or "").strip(),
    }
    if backend == "dashscope":
        data["audio_url"] = str(values.get("audio_url") or args.audio_url or "").strip()
    files = {"file": args.attachment} if backend in CLONE_UPLOAD else {}
    voice = check(await ctx.client.upload("/api/settings/voices/clone", files=files, data=data), "克隆音色")
    return ToolOutput(
        {"voice": voice},
        text=f"已克隆音色 "
        f"#{voice['id']}「{voice['name']}」voice_id={voice['voice_id']}（{voice.get('status')}）。"
        "如需设为默认音色，可用 update_business_settings(default_voice_id=…)。",
        mutation=SETTINGS,
        focus=settings_focus(section="voices"),
    )


def service_line(item: dict) -> str:
    state = "运行中" if item.get("running") else "外部运行" if item.get("external") else "已停止"
    health = "健康" if item.get("healthy") else "未就绪"
    flags = "，".join(
        label
        for key, label in (("autostart", "随后端自动启动"), ("stop_on_exit", "退出时停止"))
        if item.get(key)
    )
    detail = f"；{item['detail']}" if item.get("detail") else ""
    return (
        f"{item.get('label') or item.get('name')}（{item.get('name')}，{item.get('role') or '未注明用途'}，端"
        f"口 "
        f"{item.get('port')}）：{state}·{health}{('，' + flags) if flags else ''}{detail}"
    )


async def load_local_services(ctx: ToolContext) -> dict:
    return check(await ctx.client.get("/api/settings/local-services"), "读取本机服务")


async def list_local_services(args: Empty, ctx: ToolContext) -> ToolOutput:
    data = await load_local_services(ctx)
    items = data.get("items") or []
    if not data.get("enabled"):
        text = "本机服务管理未开启（配置文件 local_services.enabled 为 false）；桌面版缺省开启。"
    elif not items:
        text = "配置文件中没有定义本机服务（local_services.services）。"
    else:
        text = "本机服务：\n" + "\n".join(f"- {service_line(item)}" for item in items)
    return ToolOutput(
        {"enabled": bool(data.get("enabled")), "items": items},
        text=text,
        focus=settings_focus(section="services"),
    )


class LocalServiceName(StrictModel):
    name: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[a-z0-9][a-z0-9-]{0,31}$",
        description="本机服务名（list_local_services 返回的 name）",
    )


async def local_service_target(args: LocalServiceName, ctx: ToolContext) -> dict:
    data = await load_local_services(ctx)
    if not data.get("enabled"):
        raise ToolError(
            "本机服务管理未开启（配置文件 local_services.enabled 为 false）", code="local_services_disabled"
        )
    item = next((item for item in data.get("items") or [] if item.get("name") == args.name), None)
    if item is None:
        raise ToolError(f"配置文件中没有本机服务 {args.name}", code="missing_local_service")
    return item


async def preview_start_local_service(args: LocalServiceName, ctx: ToolContext) -> Preview:
    item = await local_service_target(args, ctx)
    warnings = ["会在本机启动模型服务进程，可能占用大量内存或显存；单 GPU 环境下不要同时启动多个大模型服务"]
    if item.get("external"):
        warnings.append("该端口上已有其他程序启动的服务，启动会被拒绝")
    return Preview(
        f"启动本机服务「{item.get('label') or args.name}」（端口 {item.get('port')}）",
        [{"label": "状态", "before": service_line(item), "after": "运行中"}],
        warnings=warnings,
        focus=settings_focus(section="services"),
    )


async def start_local_service(args: LocalServiceName, ctx: ToolContext) -> ToolOutput:
    await local_service_target(args, ctx)
    item = check(await ctx.client.post(f"/api/settings/local-services/{args.name}/start"), "启动本机服务")
    return ToolOutput(
        item,
        text=f"已启动：{service_line(item)}。模型加载需要一段时间，健康后即可使用。",
        mutation=SETTINGS,
        focus=settings_focus(section="services"),
    )


async def preview_stop_local_service(args: LocalServiceName, ctx: ToolContext) -> Preview:
    item = await local_service_target(args, ctx)
    warnings = ["正在使用该服务的任务（检索、转写、配音等）会失败"]
    if item.get("external"):
        warnings.append("该服务不是由本应用启动的，无法在这里停止")
    return Preview(
        f"停止本机服务「{item.get('label') or args.name}」",
        [{"label": "状态", "before": service_line(item), "after": "已停止"}],
        warnings=warnings,
        focus=settings_focus(section="services"),
    )


async def stop_local_service(args: LocalServiceName, ctx: ToolContext) -> ToolOutput:
    await local_service_target(args, ctx)
    item = check(await ctx.client.post(f"/api/settings/local-services/{args.name}/stop"), "停止本机服务")
    return ToolOutput(
        item,
        text=f"已停止：{service_line(item)}",
        mutation=SETTINGS,
        focus=settings_focus(section="services"),
    )


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "get_settings",
            "settings",
            "读取设置：各模型角色的生效配置、配置档案（ID、厂商、模型、密钥掩码）与业务设置。",
            GetSettings,
            "read",
            get_settings,
        ),
        ToolSpec(
            "list_provider_presets",
            "settings",
            "列出各厂商为各模型角色提供的预设（endpoint、模型名），用于新建档案时预填。",
            Presets,
            "read",
            list_provider_presets,
        ),
        ToolSpec(
            "list_voices", "settings", "列出音色库与当前默认音色、语音合成模型。", Empty, "read", list_voices
        ),
        ToolSpec(
            "test_model_profile",
            "settings",
            "测试某个配置档案能否连通（会对该服务发起一次最小调用；视频/图片/音乐生成档案只做鉴权/可达性探测，不发起生成）。",
            TestProfile,
            "read",
            test_model_profile,
        ),
        ToolSpec(
            "probe_model_deployment",
            "settings",
            "e2e probe：只读探测 llama_cpp "
            "嵌入/ASR/多模态理解档案的服务（/health、/props、/v1/models，不推理），"
            "报告量化、维度、能力，自动定位本机 GGUF/mmproj，并列出可复用的一致部署清单。"
            "遇到「缺少 … 部署清单」时先调用它。",
            ProbeDeployment,
            "read",
            probe_model_deployment,
        ),
        ToolSpec(
            "pin_model_deployment",
            "settings",
            "e2e pin：为 llama_cpp 档案固定 GGUF/mmproj 哈希并写入档案的 deployment_manifest。"
            "默认复用与当前部署一致的旧清单，否则自动定位本机文件计算 SHA256 生成新清单（不覆盖旧文件）；"
            "远程服务需提供文件路径或 weights_sha256/mmproj_sha256 声明。嵌入新部署还需命令行 e2e smoke。",
            PinDeployment,
            "write",
            pin_model_deployment,
            preview_pin_deployment,
        ),
        ToolSpec(
            "create_model_profile",
            "settings",
            "新建模型配置档案（不会自动激活）。api_key 只能是 ask_user_question secret 字段返回的 secret:// "
            "引用。"
            "视频生成（video_generator）档案：backend 为 minimax_h3（MiniMax H3，只需 base_url "
            "与密钥，生成方式在分镜设置）"
            "/minimax/dashscope/comfyui；图片生成（image_generator，MiniMax "
            "image-01）与音乐生成（music_generator，"
            "MiniMax music-3.0）的 backend 为 minimax/comfyui（音乐生成另有 audiocpp：本地 audio.cpp 的 "
            "MiniMax-Music3-GGUF，不支持翻唱与歌词生成）；comfyui 需 comfyui_workflow（API 格式）与 "
            "comfyui_mapping。",
            CreateProfile,
            "write",
            create_model_profile,
            preview_create_profile,
        ),
        ToolSpec(
            "update_business_settings",
            "settings",
            "修改业务设置：GPU 互斥、检索权重、默认音色。",
            Business,
            "write",
            update_business_settings,
            preview_business,
        ),
        ToolSpec(
            "update_model_profile",
            "settings",
            "修改已有配置档案（只传要改的字段；api_key 为 secret:// 引用或 "
            "__clear__）。不能修改智能助手正在使用的档案。",
            UpdateProfile,
            "danger",
            update_model_profile,
            preview_update_profile,
        ),
        ToolSpec(
            "activate_model_profile",
            "settings",
            "激活配置档案，立即热生效。不能切换智能助手自身的档案。",
            ActivateProfile,
            "danger",
            activate_model_profile,
            preview_activate_profile,
        ),
        ToolSpec(
            "delete_model_profile",
            "settings",
            "删除配置档案（不可恢复）。不能删除各角色正在使用的档案，也不能删除智能助手自身的档案。",
            DeleteProfile,
            "danger",
            delete_model_profile,
            preview_delete_profile,
        ),
        ToolSpec(
            "reset_business_settings",
            "settings",
            "重置业务设置：清除页面保存的 GPU 互斥、检索调参与默认音色覆盖，恢复配置文件中的值。",
            Empty,
            "danger",
            reset_business_settings,
            preview_reset_business,
        ),
        ToolSpec(
            "list_voice_catalog",
            "settings",
            "查询当前语音合成后端的音色目录：系统音色分组、音色库、默认音色与是否支持克隆。",
            Empty,
            "read",
            list_voice_catalog,
        ),
        ToolSpec(
            "register_voice",
            "settings",
            "把已有的厂商音色（voice_id）登记到音色库。",
            RegisterVoice,
            "write",
            register_voice,
            preview_register_voice,
        ),
        ToolSpec(
            "delete_voice",
            "settings",
            "从音色库删除一条音色记录（按记录 ID）；若它是默认音色，默认音色覆盖会一并清除。",
            DeleteVoice,
            "danger",
            delete_voice,
            preview_delete_voice,
        ),
        ToolSpec(
            "clone_voice",
            "settings",
            "克隆音色：openai/minimax 后端需要声音样本附件（attachment://），dashscope 后端使用公网样本 URL。"
            "执行时会弹出表单，由用户本人确认声音授权；你无法代为授权。仅在 Mindex 页面助手中可用。",
            CloneVoice,
            "danger",
            clone_voice,
            preview_clone_voice,
        ),
        ToolSpec(
            "list_local_services",
            "settings",
            "列出本机模型服务（配置文件 local_services 中定义的 WeMM、ASR、TTS "
            "等）：运行状态、健康、端口与自启设置。",
            Empty,
            "read",
            list_local_services,
        ),
        ToolSpec(
            "start_local_service",
            "settings",
            "启动一个本机模型服务（只能启动配置文件中定义的服务，不能改命令）。会占用本机内存/显存。",
            LocalServiceName,
            "danger",
            start_local_service,
            preview_start_local_service,
        ),
        ToolSpec(
            "stop_local_service",
            "settings",
            "停止一个由本应用启动的本机模型服务；正在使用它的任务会失败。",
            LocalServiceName,
            "danger",
            stop_local_service,
            preview_stop_local_service,
        ),
    ]
