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

from mindex.agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import check
from mindex.shared.config import StrictModel

ROLE_NAMES = Literal["analyzer", "writer", "generator", "embedding", "asr", "tts", "person", "agent"]
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
    raise ToolError("api_key 必须是 ask_user_question 的 secret 字段返回的 secret:// 引用；不要让用户在对话里提供密钥",
                    code="plaintext_secret")


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
            "role": role["role"], "label": role["label"], "active_profile_id": role["active_profile_id"],
            "effective": {k: effective.get(k) for k in ("backend", "base_url", "model", "max_tokens", "voice")
                          if effective.get(k) is not None},
            "api_key": key_text(effective["api_key"]) if not effective.get("env_key_set") else "使用环境变量",
            "profiles": [{"id": p["id"], "name": p["name"], "provider": p["provider"], "model": p["config"].get("model"),
                          "base_url": p["config"].get("base_url"), "api_key": key_text(p["api_key"]),
                          "active": p["id"] == role["active_profile_id"]} for p in role["profiles"]],
        }
        roles.append(item)
        lines.append(f"{role['label']}({role['role']})：{effective['model']} · {effective['backend']} · "
                     f"{effective['base_url']} · 密钥{item['api_key']}")
        for profile in item["profiles"]:
            lines.append(f"  - 档案 #{profile['id']} {profile['name']} [{profile['provider']}] {profile['model']}"
                         f"{' 【激活】' if profile['active'] else ''}")
    value = {"roles": roles, "business": data["business"], "providers": data["providers"]}
    if not args.role:
        business = data["business"]
        lines.append(f"业务设置：GPU 互斥={business['enforce_gpu_lock']}，默认音色={business['default_voice'] or '跟随语音合成档案'}，"
                     f"检索三渠道权重={business['search']['channel_weights']}，rrf_k={business['search']['rrf_k']}，"
                     f"候选数={business['search']['candidates']}")
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
            rows.append({"provider": provider, "provider_label": data["providers"].get(provider, provider),
                         "role": role, "config": preset["config"], "note": preset.get("note", "")})
    text = "\n".join(f"{r['provider']}({r['provider_label']}) · {r['role']}：{r['config']} {r['note']}" for r in rows)
    return ToolOutput({"presets": rows, "providers": data["providers"]}, text=text or "（没有匹配的预设）",
                      focus=settings_focus(role=args.role, section=None if args.role else "models"))


async def list_voices(args: Empty, ctx: ToolContext) -> ToolOutput:
    data = await load_settings(ctx)
    voices = [{"id": v["id"], "name": v["name"], "voice_id": v["voice_id"], "provider": v["provider"], "kind": v["kind"],
               "status": v["status"]} for v in data["voices"]]
    business = data["business"]
    tts = next(r for r in data["roles"] if r["role"] == "tts")["effective"]
    text = (f"语音合成：{tts['model']}（{business['tts_backend']}），档案音色 {tts.get('voice')}；"
            f"默认音色覆盖：{business['default_voice'] or '无'}\n音色库：\n"
            + ("\n".join(f"#{v['id']} {v['name']} voice_id={v['voice_id']} [{v['provider']}/{v['kind']}] {v['status']}"
                         for v in voices) or "（空）"))
    return ToolOutput({"voices": voices, "default_voice": business["default_voice"], "tts": tts}, text=text,
                      focus=settings_focus(section="voices"))


class TestProfile(StrictModel):
    profile_id: int = Field(description="配置档案 ID")


async def test_model_profile(args: TestProfile, ctx: ToolContext) -> ToolOutput:
    role = role_of(await load_settings(ctx), args.profile_id)
    result = check(await ctx.client.post(f"/api/settings/profiles/{args.profile_id}/test"), "测试连接")
    text = (f"连接成功，耗时 {result['latency_ms']}ms" + (f"，{result['reply']}" if result.get("reply") else "")
            if result["ok"] else f"连接失败：{result.get('detail')}")
    return ToolOutput(result, text=text, focus=settings_focus(role=role, profile_id=args.profile_id))


class ProfileConfig(StrictModel):
    backend: str | None = Field(default=None, description="协议后端，例如 openai、anthropic、mindex、llama_cpp")
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


class CreateProfile(StrictModel):
    role: ROLE_NAMES
    name: str = Field(min_length=1, max_length=60)
    provider: Literal["custom", "self", "zhipu", "deepseek", "aliyun", "tencent", "minimax", "anthropic"] = "custom"
    config: ProfileConfig
    api_key: str | None = Field(default=None, description="只能是 secret:// 引用")


async def preview_create_profile(args: CreateProfile, ctx: ToolContext) -> Preview:
    check_key(args.api_key)
    config = args.config.model_dump(exclude_none=True)
    changes = [{"label": key, "after": value} for key, value in config.items()]
    changes.append({"label": "API Key", "after": "已通过表单提供" if args.api_key else "未提供"})
    return Preview(f"新建「{args.role}」角色的配置档案「{args.name}」（不会自动激活）", changes,
                   focus=settings_focus(role=args.role))


async def create_model_profile(args: CreateProfile, ctx: ToolContext) -> ToolOutput:
    check_key(args.api_key)
    body = {"role": args.role, "name": args.name, "provider": args.provider,
            "config": args.config.model_dump(exclude_none=True)}
    if args.api_key:
        body["api_key"] = args.api_key  # 回环客户端在发送前解引用
    profile = check(await ctx.client.post("/api/settings/profiles", body), "新建配置档案")
    return ToolOutput({"profile_id": profile["id"], "api_key": profile["api_key"]},
                      text=f"已新建档案 #{profile['id']}「{profile['name']}」，密钥{key_text(profile['api_key'])}。"
                           "可先 test_model_profile 测试连接，再 activate_model_profile 激活。",
                      mutation=SETTINGS, focus=settings_focus(role=args.role, profile_id=profile["id"]))


class UpdateProfile(StrictModel):
    profile_id: int
    name: str | None = Field(default=None, min_length=1, max_length=60)
    config: ProfileConfig | None = Field(default=None, description="只填需要修改的字段，其余保持不变")
    api_key: str | None = Field(default=None, description="secret:// 引用；__clear__ 清除；省略保持原密钥")


async def profile_body(args: UpdateProfile, ctx: ToolContext):
    check_key(args.api_key)
    if ctx.agent_profile_id is not None and args.profile_id == ctx.agent_profile_id:
        raise ToolError("不能修改智能助手当前正在使用的档案（会影响本会话自身）；请在设置页修改", code="self_profile")
    data = await load_settings(ctx)
    role, profile = find_profile(data, args.profile_id)
    config = {**profile["config"], **(args.config.model_dump(exclude_none=True) if args.config else {})}
    body = {"role": role["role"], "name": args.name or profile["name"], "provider": profile["provider"], "config": config}
    if args.api_key:
        body["api_key"] = args.api_key
    return role, profile, body


async def preview_update_profile(args: UpdateProfile, ctx: ToolContext) -> Preview:
    role, profile, body = await profile_body(args, ctx)
    changes = [{"label": key, "before": profile["config"].get(key), "after": value}
               for key, value in body["config"].items() if profile["config"].get(key) != value]
    if body["name"] != profile["name"]:
        changes.append({"label": "名称", "before": profile["name"], "after": body["name"]})
    if args.api_key:
        changes.append({"label": "API Key", "before": key_text(profile["api_key"]),
                        "after": "清除" if args.api_key == CLEAR_KEY else "替换为表单提供的新密钥"})
    warnings = ["该档案正在使用，修改立即生效"] if role["active_profile_id"] == profile["id"] else []
    return Preview(f"修改{role['label']}档案「{profile['name']}」", changes, warnings=warnings,
                   focus=settings_focus(role=role["role"], profile_id=profile["id"]))


async def update_model_profile(args: UpdateProfile, ctx: ToolContext) -> ToolOutput:
    role, _, body = await profile_body(args, ctx)
    profile = check(await ctx.client.put(f"/api/settings/profiles/{args.profile_id}", body), "修改配置档案")
    return ToolOutput({"profile_id": profile["id"]}, text=f"已修改档案 #{profile['id']}「{profile['name']}」。",
                      mutation=SETTINGS, focus=settings_focus(role=role["role"], profile_id=profile["id"]))


class ActivateProfile(StrictModel):
    profile_id: int


async def activation_target(args: ActivateProfile, ctx: ToolContext):
    data = await load_settings(ctx)
    role, profile = find_profile(data, args.profile_id)
    if role["role"] == "agent" and ctx.agent_profile_id is not None and args.profile_id != ctx.agent_profile_id:
        raise ToolError("不能在会话中切换智能助手自身的模型档案（当前会话会失去原模型）；请在设置页切换", code="self_profile")
    return role, profile


async def preview_activate_profile(args: ActivateProfile, ctx: ToolContext) -> Preview:
    role, profile = await activation_target(args, ctx)
    current = next((p["name"] for p in role["profiles"] if p["id"] == role["active_profile_id"]), "YAML 基础配置")
    warnings = ["保存即热生效：之后的任务与请求都会使用新模型"]
    if role["role"] == "embedding":
        warnings.append("切换嵌入模型后，若身份与已发布索引不一致，检索将不可用，直至重建并发布索引")
    if role["role"] in ("generator", "writer", "tts"):
        warnings.append("已有创作产物不受影响；下次构建时受影响的节点会重建")
    return Preview(f"激活{role['label']}档案", [{"label": role["label"], "before": current, "after": profile["name"]}],
                   warnings=warnings, focus=settings_focus(role=role["role"], profile_id=profile["id"]))


async def activate_model_profile(args: ActivateProfile, ctx: ToolContext) -> ToolOutput:
    role, profile = await activation_target(args, ctx)
    result = check(await ctx.client.post(f"/api/settings/profiles/{args.profile_id}/activate"), "激活配置档案")
    text = f"已激活{role['label']}档案「{profile['name']}」。"
    if result.get("index_warning"):
        text += "警告：" + result["index_warning"]["message"]
    return ToolOutput(result, text=text, mutation=SETTINGS,
                      focus=settings_focus(role=role["role"], profile_id=profile["id"]))


class SearchTuning(StrictModel):
    channel_weights: dict[str, float] | None = Field(default=None, description="新版三渠道权重 visual/dialogue/analysis")
    weights: dict[str, float] | None = Field(default=None, description="旧版五路权重 video/description/style/dialogue/lexical")
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
        changes.append({"label": "GPU 互斥", "before": current["enforce_gpu_lock"], "after": args.enforce_gpu_lock})
    if args.search:
        for key, value in args.search.model_dump(exclude_none=True).items():
            changes.append({"label": f"检索 {key}", "before": current["search"].get(key), "after": value})
    if args.clear_default_voice:
        changes.append({"label": "默认音色", "before": current["default_voice"], "after": "清除"})
    elif args.default_voice or args.default_voice_id is not None:
        changes.append({"label": "默认音色", "before": current["default_voice"],
                        "after": args.default_voice or f"音色库 #{args.default_voice_id}"})
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


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("get_settings", "settings", "读取设置：各模型角色的生效配置、配置档案（ID、厂商、模型、密钥掩码）与业务设置。",
                 GetSettings, "read", get_settings),
        ToolSpec("list_provider_presets", "settings", "列出各厂商为各模型角色提供的预设（endpoint、模型名），用于新建档案时预填。",
                 Presets, "read", list_provider_presets),
        ToolSpec("list_voices", "settings", "列出音色库与当前默认音色、语音合成模型。", Empty, "read", list_voices),
        ToolSpec("test_model_profile", "settings", "测试某个配置档案能否连通（会对该服务发起一次最小调用）。",
                 TestProfile, "read", test_model_profile),
        ToolSpec("create_model_profile", "settings",
                 "新建模型配置档案（不会自动激活）。api_key 只能是 ask_user_question secret 字段返回的 secret:// 引用。",
                 CreateProfile, "write", create_model_profile, preview_create_profile),
        ToolSpec("update_business_settings", "settings", "修改业务设置：GPU 互斥、检索权重、默认音色。",
                 Business, "write", update_business_settings, preview_business),
        ToolSpec("update_model_profile", "settings",
                 "修改已有配置档案（只传要改的字段；api_key 为 secret:// 引用或 __clear__）。不能修改智能助手正在使用的档案。",
                 UpdateProfile, "danger", update_model_profile, preview_update_profile),
        ToolSpec("activate_model_profile", "settings",
                 "激活配置档案，立即热生效。不能切换智能助手自身的档案。", ActivateProfile, "danger",
                 activate_model_profile, preview_activate_profile),
    ]
