"""运行时设置：模型服务配置档案、热生效覆盖层与密钥掩码。

- 配置档案（profile）按角色存 SQLite，激活后由 apply_overrides 合成「生效配置」；
  Web 与 CLI 共用同一覆盖层，保存即热生效，无需重启。
- 覆盖层只允许白名单字段（服务连接段 + 检索调参 + 默认音色），
  绝不触碰 data_dir 与切分布局相关字段。
- API Key 明文存 model_profiles.api_key，仅在请求头中使用；对外只输出掩码，
  且 Service.api_key 带 exclude=True，任何 model_dump()/指纹都不会携带。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from mindex.shared.comfyui import validate_workflow
from mindex.shared.config import (
    ANIMATOR_INHERITED,
    H3_DURATIONS,
    AgentLlmConfig,
    AnalyzerConfig,
    AnimatorConfig,
    ASRConfig,
    Config,
    EmbeddingConfig,
    GeneratorConfig,
    ImageGeneratorConfig,
    MusicGeneratorConfig,
    PersonConfig,
    SearchConfig,
    SpeakerConfig,
    VideoGeneratorConfig,
)
from mindex.shared.storage import Store

ROLES = (
    "analyzer", "writer", "generator", "embedding", "asr", "tts", "image_generator", "video_generator",
    "music_generator", "animator", "person", "agent",
)
ROLE_LABELS = {
    "agent": "智能助手",
    "person": "人物识别",
    "analyzer": "多模态理解",
    "writer": "文案创作",
    "generator": "文本拆分镜",
    "embedding": "多模态嵌入",
    "asr": "语音识别",
    "tts": "语音合成",
    "image_generator": "图片生成",
    "video_generator": "视频生成",
    "music_generator": "音乐生成",
    "animator": "动画生成",
}

# 各角色允许 profile 覆盖的字段；未列字段永远承袭 yaml 原值。
PROFILE_FIELDS = {
    "person": {
        "backend", "base_url", "model", "revision", "timeout", "api_key_env", "expected_space",
        "fps", "frame_interval", "face_threshold", "face_margin", "min_face_quality", "min_reference_quality", "body_threshold",
    },
    "analyzer": {
        "backend", "base_url", "model", "revision", "timeout", "api_key_env", "llama_template",
        "response_retries", "initial_output_tokens", "max_output_tokens", "thinking_mode", "deployment_manifest",
    },
    "generator": {"backend", "base_url", "model", "revision", "timeout", "api_key_env", "llama_template", "max_tokens"},
    "writer": {"backend", "base_url", "model", "revision", "timeout", "api_key_env", "llama_template", "max_tokens"},
    "agent": {"backend", "base_url", "model", "revision", "timeout", "api_key_env", "max_tokens", "vision"},
    # deployment_manifest：llama_cpp 部署清单（e2e pin 生成）；相对路径按数据目录解析。
    "embedding": {
        "backend", "base_url", "model", "revision", "timeout", "api_key_env", "quantization", "dimensions",
        "deployment_manifest",
    },
    "asr": {
        "backend", "base_url", "model", "revision", "timeout", "api_key_env", "enabled", "max_tokens",
        "deployment_manifest",
    },
    "tts": {
        "backend",
        "base_url",
        "model",
        "revision",
        "timeout",
        "api_key_env",
        "speech_path",
        "response_format",
        "voice",
    },
    # enabled 不在白名单：激活档案即启用视频生成（merge_profile 置 True）。
    "video_generator": {
        "backend", "base_url", "model", "revision", "timeout", "api_key_env", "poll_interval", "max_wait",
        "durations", "fps", "comfyui_workflow", "comfyui_mapping",
    },
    # 图片/音乐生成同理：激活档案即启用。
    "image_generator": {
        "backend", "base_url", "model", "revision", "timeout", "api_key_env", "poll_interval", "max_wait",
        "comfyui_workflow", "comfyui_mapping",
    },
    "music_generator": {
        "backend", "base_url", "model", "cover_model", "revision", "timeout", "api_key_env", "poll_interval",
        "max_wait", "comfyui_workflow", "comfyui_mapping", "num_inference_steps", "max_seconds",
    },
    # 同上：激活动画生成档案即启用（merge_profile 置 enabled=True）。
    "animator": {
        "backend", "base_url", "model", "revision", "timeout", "api_key_env", "max_tokens", "retries",
        "max_capture_seconds", "max_html_kb",
    },
}
SECTION_TYPES = {
    "person": PersonConfig,
    "analyzer": AnalyzerConfig,
    "generator": GeneratorConfig,
    "writer": GeneratorConfig,
    "embedding": EmbeddingConfig,
    "asr": ASRConfig,
    "tts": SpeakerConfig,
    "video_generator": VideoGeneratorConfig,
    "image_generator": ImageGeneratorConfig,
    "music_generator": MusicGeneratorConfig,
    "animator": AnimatorConfig,
    "agent": AgentLlmConfig,
}
_STUDIO_ROLES = {"generator", "writer", "tts", "video_generator", "image_generator", "music_generator", "animator"}
# 激活档案即启用的角色（无档案时沿用 yaml 的 enabled）。
_ENABLING_ROLES = ("video_generator", "image_generator", "music_generator", "animator")

PROVIDERS = ("custom", "self", "zhipu", "deepseek", "aliyun", "tencent", "minimax", "anthropic")
PROVIDER_LABELS = {
    "custom": "自定义",
    "self": "自建服务",
    "zhipu": "智谱",
    "deepseek": "DeepSeek",
    "aliyun": "阿里云百炼",
    "tencent": "腾讯云",
    "minimax": "MiniMax",
    "anthropic": "Anthropic",
}

# 线上厂商预设：均为「最佳已知」预填值，模型名会随平台演进漂移，页面可改 + 测试连接兜底。
# 未列出的角色表示该厂商不提供（如智谱无多模态嵌入/ASR/TTS）。
PROVIDER_PRESETS: dict[str, dict[str, dict]] = {
    "zhipu": {
        "analyzer": {
            "config": {
                "backend": "openai",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4.5v",
                "llama_template": False,
            },
            "note": "GLM-4.5V 视觉理解，OpenAI 兼容",
        },
        "generator": {
            "config": {
                "backend": "openai",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4.6",
                "llama_template": False,
            },
            "note": "文本模型，名称以平台模型列表为准",
        },
    },
    "deepseek": {
        "analyzer": {
            "config": {
                "backend": "openai",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-v4-flash-vision-exp",
                "llama_template": False,
            },
            "note": "视觉理解模型，名称以平台模型列表为准",
        },
        "generator": {
            "config": {
                "backend": "openai",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "llama_template": False,
            },
            "note": "纯文本拆分镜的性价比之选",
        },
    },
    "aliyun": {
        "analyzer": {
            "config": {
                "backend": "openai",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen3-vl-plus",
                "llama_template": False,
            },
            "note": "Qwen-VL 系列，名称以百炼模型列表为准",
        },
        "generator": {
            "config": {
                "backend": "openai",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen-plus",
                "llama_template": False,
            },
            "note": "Qwen 文本系列",
        },
        "asr": {
            "config": {
                "backend": "openai",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen3-asr-flash",
            },
            "note": "OpenAI 兼容 chat + input_audio；音频 ≤10MB/5 分钟",
        },
        "tts": {
            "config": {
                "backend": "dashscope",
                "base_url": "https://dashscope.aliyuncs.com",
                "model": "qwen3-tts-flash",
            },
            "note": "DashScope 原生协议专用适配器",
        },
        "video_generator": {
            "config": {
                "backend": "dashscope",
                "base_url": "https://dashscope.aliyuncs.com",
                "model": "wan2.6-t2v",
                "durations": [5.0, 10.0, 15.0],
            },
            "note": "通义万相异步任务；有参考图的分镜需改用图生视频模型（如 wan2.6-i2v-flash）",
        },
    },
    "tencent": {
        "analyzer": {
            "config": {
                "backend": "openai",
                "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
                "model": "hunyuan-turbo-vision",
                "llama_template": False,
            },
            "note": "混元视觉理解，OpenAI 兼容；模型名以控制台为准",
        },
        "generator": {
            "config": {
                "backend": "openai",
                "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
                "model": "hunyuan-turbo",
                "llama_template": False,
            },
            "note": "混元文本模型",
        },
        "embedding": {
            "config": {
                "backend": "tokenhub",
                "base_url": "https://tokenhub.tencentcloudmaas.com/v1",
                "model": "kinfra-vl-embedding-8b",
                "dimensions": 4096,
                "quantization": "cloud",
            },
            "note": "WeMM 系多模态向量，文本/图片/视频统一空间；另有 2b 档（2048 维）",
        },
    },
    "minimax": {
        "generator": {
            "config": {
                "backend": "openai",
                "base_url": "https://api.minimax.cn/v1",
                "model": "MiniMax-M2",
                "llama_template": False,
            },
            "note": "文本系列，名称以平台模型列表为准",
        },
        "tts": {
            "config": {
                "backend": "minimax",
                "base_url": "https://api.minimax.cn/v1",
                "model": "speech-2.8-hd",
                "voice": "male-qn-qingse",
            },
            "note": "t2a_v2 专用适配器；预填系统音色，克隆后可换自定义音色 ID",
        },
        "asr": {
            "config": {
                "backend": "minimax",
                "base_url": "https://api.minimax.cn/v1",
                "model": "asr-1.0",
            },
            "note": "speech_to_text 专用协议；音频 ≤500 秒/50MB，30 秒切块远小于上限",
        },
        "video_generator": {
            "config": {
                "backend": "minimax_h3",
                "base_url": "https://api.minimax.cn/v1",
                "model": "MiniMax-H3",
                "durations": list(H3_DURATIONS),
            },
            "note": "MiniMax H3 视频生成 V2：设置页只需 endpoint 与 API Key；文生/首帧/尾帧/首尾帧/主体参考、"
                    "模型档位、分辨率与时长在创作页逐镜设置",
        },
        "image_generator": {
            "config": {"backend": "minimax", "base_url": "https://api.minimax.cn/v1", "model": "image-01"},
            "note": "image-01 文生图 / 主体参考图生图（/v1/image_generation）",
        },
        "music_generator": {
            "config": {"backend": "minimax", "base_url": "https://api.minimax.cn/v1", "model": "music-3.0",
                       "cover_model": "music-cover"},
            "note": "music-3.0 音乐生成、music-cover 翻唱与歌词生成；2026-08-20 起仅对历史付费用户开放",
        },
    },
}
# 本地 ComfyUI：导入「导出（API）」工作流与节点映射后可用（视频/图片/音乐生成）。
COMFYUI_PRESETS = {
    "video_generator": {"backend": "comfyui", "base_url": "http://127.0.0.1:8188", "model": "", "durations": [5.0],
                        "fps": 16},
    "image_generator": {"backend": "comfyui", "base_url": "http://127.0.0.1:8188", "model": ""},
    "music_generator": {"backend": "comfyui", "base_url": "http://127.0.0.1:8188", "model": ""},
}

# 文案创作与拆分镜同为文本聊天模型：各厂商的文案预设直接复制拆分镜预设。
for _roles in PROVIDER_PRESETS.values():
    if "generator" in _roles and "writer" not in _roles:
        _roles["writer"] = {
            "config": dict(_roles["generator"]["config"]),
            "note": _roles["generator"]["note"].replace("纯文本拆分镜的性价比之选", "文本模型，性价比之选"),
        }

# 智能助手需要稳定的多轮工具调用：只为确认支持 function calling 的文本模型提供预设。
_AGENT_PRESETS = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", "OpenAI 兼容工具调用；名称以平台模型列表为准"),
    "aliyun": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus", "Qwen 工具调用，OpenAI 兼容"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4.6", "GLM 工具调用，OpenAI 兼容"),
}
for _provider, (_url, _model, _note) in _AGENT_PRESETS.items():
    PROVIDER_PRESETS[_provider]["agent"] = {
        "config": {"backend": "openai", "base_url": _url, "model": _model},
        "note": _note,
    }
PROVIDER_PRESETS["anthropic"] = {
    "agent": {
        "config": {"backend": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-sonnet-4-5"},
        "note": "Anthropic 原生 Messages 协议；模型名以控制台列表为准",
    }
}

# 动画生成与智能助手同协议（OpenAI 兼容 / Anthropic Messages）：沿用智能助手的厂商预设，另需较大的 max_tokens。
for _roles in PROVIDER_PRESETS.values():
    if "agent" in _roles:
        _roles["animator"] = {
            "config": {**_roles["agent"]["config"], "max_tokens": 16000},
            "note": "写 HTML/JS 动画源码，与智能助手同协议；源码较长，max_tokens 宜 ≥ 8000",
        }

# 动画生成的默认模型：智谱 GLM-5.3-Flash（OpenAI 兼容）；档案不填密钥时复用多模态理解里同名模型档案的密钥。
PROVIDER_PRESETS["zhipu"]["animator"] = {
    "config": {"backend": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-5.3-flash",
               "max_tokens": 16000},
    "note": "GLM-5.3-Flash 写 HTML/JS 动画源码（默认）；OpenAI 兼容协议；不填密钥时复用多模态理解里同名模型档案的密钥与地址",
}

# 设置页的默认档案卡：首次启动时为下列角色各建一张（已有档案的角色不建），第一张激活为默认生效配置。
DEFAULT_PROFILES = {
    "animator": [("GLM-5.3-Flash（默认）", "zhipu", PROVIDER_PRESETS["zhipu"]["animator"]["config"])],
    "video_generator": [("MiniMax H3（默认）", "minimax", PROVIDER_PRESETS["minimax"]["video_generator"]["config"]),
                        ("本地 ComfyUI", "self", COMFYUI_PRESETS["video_generator"])],
    "image_generator": [("MiniMax image-01（默认）", "minimax", PROVIDER_PRESETS["minimax"]["image_generator"]["config"]),
                        ("本地 ComfyUI", "self", COMFYUI_PRESETS["image_generator"])],
    "music_generator": [("MiniMax music-3.0（默认）", "minimax", PROVIDER_PRESETS["minimax"]["music_generator"]["config"]),
                        ("本地 ComfyUI", "self", COMFYUI_PRESETS["music_generator"])],
}

# 显式清除已存密钥的哨兵值（空字符串视为未填、保留原值）。
CLEAR_KEY = "__clear__"


def mask_key(key: str | None) -> dict:
    """对外只暴露「是否已设置 + 尾 4 位」，绝不回传完整密钥。"""
    if not key:
        return {"has_key": False, "hint": ""}
    return {"has_key": True, "hint": "…" + key[-4:] if len(key) > 8 else "…"}


def get_setting(store: Store, key: str) -> str | None:
    row = store.one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else None


def put_setting(store: Store, key: str, value: str) -> None:
    store.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))


VOICE_PROVIDERS = ("minimax", "dashscope", "self", "openai")


def save_voice(
    store: Store,
    *,
    name: str,
    provider: str,
    voice_id: str,
    kind: str = "preset",
    sample_path: str | None = None,
    status: str = "ready",
    notes: str = "",
    voice_pk: int | None = None,
) -> dict:
    """新建或更新音色记录；克隆流程用 status=pending/failed 标注进展。"""
    if provider not in VOICE_PROVIDERS:
        raise ValueError(f"未知音色厂商 {provider}；可选 {list(VOICE_PROVIDERS)}")
    if kind not in {"preset", "clone"}:
        raise ValueError("kind 仅支持 preset/clone")
    if not name.strip() or not voice_id.strip():
        raise ValueError("音色名称与 voice_id 不能为空")
    try:
        with store.connect() as db:
            if voice_pk is None:
                cursor = db.execute(
                    "INSERT INTO voices(name,kind,provider,voice_id,sample_path,status,notes)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (name.strip(), kind, provider, voice_id.strip(), sample_path, status, notes),
                )
                voice_pk = cursor.lastrowid
            else:
                db.execute(
                    "UPDATE voices SET name=?,kind=?,provider=?,voice_id=?,sample_path=?,"
                    "status=?,notes=? WHERE id=?",
                    (name.strip(), kind, provider, voice_id.strip(), sample_path, status, notes, voice_pk),
                )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"已存在同名音色「{name}」") from exc
    return get_voice(store, voice_pk)


def _voice_row(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "provider": row["provider"],
        "voice_id": row["voice_id"],
        "sample_path": row["sample_path"],
        "status": row["status"],
        "notes": row["notes"],
        "created_at": row["created_at"],
    }


def get_voice(store: Store, voice_pk: int) -> dict | None:
    row = store.one("SELECT * FROM voices WHERE id=?", (voice_pk,))
    return _voice_row(row) if row else None


def list_voices(store: Store) -> list[dict]:
    return [_voice_row(row) for row in store.rows("SELECT * FROM voices ORDER BY created_at,id")]


def delete_voice(store: Store, voice_pk: int) -> None:
    if store.one("SELECT 1 FROM voices WHERE id=?", (voice_pk,)) is None:
        raise ValueError(f"音色 {voice_pk} 不存在")
    store.execute("DELETE FROM voices WHERE id=?", (voice_pk,))


def _validate_fields(role: str, fields: dict) -> None:
    unknown = set(fields) - PROFILE_FIELDS[role]
    if unknown:
        raise ValueError(f"配置档案不支持字段 {sorted(unknown)}；角色 {role} 仅允许 {sorted(PROFILE_FIELDS[role])}")
    if role == "video_generator" and (
        fields.get("comfyui_workflow") is not None or fields.get("comfyui_mapping") is not None
    ):
        # 先给出纯中文的工作流/映射错误，再做完整的字段校验。
        if fields.get("comfyui_workflow") is None:
            raise ValueError("已填写 ComfyUI 节点映射，但没有导入工作流")
        validate_workflow(fields["comfyui_workflow"], fields.get("comfyui_mapping") or {})
    SECTION_TYPES[role].model_validate({**SECTION_TYPES[role]().model_dump(), **fields})


def _row_to_profile(row: dict) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "name": row["name"],
        "provider": row["provider"],
        "config": json.loads(row["config"]),
        "api_key": mask_key(row["api_key"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_profile(
    store: Store,
    *,
    role: str,
    name: str,
    provider: str,
    config: dict,
    api_key: str | None = None,
    profile_id: int | None = None,
) -> dict:
    """新建或更新档案。api_key 三态：None=保留原值，CLEAR_KEY=清除，其余非空串=新值。"""
    if role not in ROLES:
        raise ValueError(f"未知角色 {role}；可选 {list(ROLES)}")
    if provider not in PROVIDERS:
        raise ValueError(f"未知厂商 {provider}；可选 {list(PROVIDERS)}")
    if not name.strip():
        raise ValueError("档案名称不能为空")
    _validate_fields(role, config)
    try:
        with store.connect() as db:
            if profile_id is None:
                cursor = db.execute(
                    "INSERT INTO model_profiles(role,name,provider,config,api_key) VALUES(?,?,?,?,?)",
                    (role, name.strip(), provider, json.dumps(config, ensure_ascii=False), api_key or None),
                )
                profile_id = cursor.lastrowid
            else:
                if api_key is None:  # 保留原值
                    db.execute(
                        "UPDATE model_profiles SET role=?,name=?,provider=?,config=?,"
                        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (role, name.strip(), provider, json.dumps(config, ensure_ascii=False), profile_id),
                    )
                else:
                    db.execute(
                        "UPDATE model_profiles SET role=?,name=?,provider=?,config=?,api_key=?,"
                        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (
                            role,
                            name.strip(),
                            provider,
                            json.dumps(config, ensure_ascii=False),
                            None if api_key == CLEAR_KEY or not api_key.strip() else api_key,
                            profile_id,
                        ),
                    )
                if db.total_changes == 0 or not db.execute(
                    "SELECT 1 FROM model_profiles WHERE id=?", (profile_id,)
                ).fetchone():
                    raise ValueError(f"配置档案 {profile_id} 不存在")
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"角色 {ROLE_LABELS.get(role, role)} 已存在同名档案「{name}」") from exc
    row = store.one("SELECT * FROM model_profiles WHERE id=?", (profile_id,))
    return _row_to_profile(row)


def list_profiles(store: Store, role: str | None = None) -> list[dict]:
    if role is not None:
        if role not in ROLES:
            raise ValueError(f"未知角色 {role}")
        rows = store.rows("SELECT * FROM model_profiles WHERE role=? ORDER BY name,id", (role,))
    else:
        rows = store.rows("SELECT * FROM model_profiles ORDER BY role,name,id")
    return [_row_to_profile(row) for row in rows]


def delete_profile(store: Store, profile_id: int) -> None:
    row = store.one("SELECT role, name FROM model_profiles WHERE id=?", (profile_id,))
    if row is None:
        raise ValueError(f"配置档案 {profile_id} 不存在")
    if get_setting(store, f"active.profile.{row['role']}") == str(profile_id):
        raise ValueError("无法删除正在使用的档案，请先切换到其他档案")
    store.execute("DELETE FROM model_profiles WHERE id=?", (profile_id,))


def activate_profile(store: Store, profile_id: int) -> dict:
    row = store.one("SELECT id, role, name FROM model_profiles WHERE id=?", (profile_id,))
    if row is None:
        raise ValueError(f"配置档案 {profile_id} 不存在")
    put_setting(store, f"active.profile.{row['role']}", str(profile_id))
    return {"role": row["role"], "profile_id": profile_id, "name": row["name"]}


def _section(config: Config, role: str):
    if role == "agent":
        return config.agent.llm
    if role == "generator":
        return config.studio.generator
    if role == "writer":
        return config.studio.writer
    if role == "tts":
        return config.studio.tts
    if role == "video_generator":
        return config.studio.video_generator
    if role == "animator":
        return config.studio.animator
    if role in ("image_generator", "music_generator"):
        return getattr(config.studio, role)
    return getattr(config, role)


def _active_profile(store: Store, role: str) -> dict | None:
    pointer = get_setting(store, f"active.profile.{role}")
    if pointer is None:
        return None
    row = store.one("SELECT * FROM model_profiles WHERE id=?", (int(pointer),))
    if row is None:  # 悬垂指针：防御式回退到基础配置
        return None
    return {**row, "config": json.loads(row["config"])}


def merge_profile(config: Config, role: str, profile: dict) -> Config:
    """把单个档案套到配置上（无论是否激活）；返回新 Config，绝不修改原对象。"""
    current = _section(config, role)
    fields = {key: value for key, value in profile["config"].items() if key in PROFILE_FIELDS[role]}
    manifest = fields.get("deployment_manifest")
    if isinstance(manifest, str) and manifest and not Path(manifest).expanduser().is_absolute():
        fields["deployment_manifest"] = str(Path(config.data_dir) / manifest)
    fields["api_key"] = profile.get("api_key") or ""
    if role in _ENABLING_ROLES:
        fields["enabled"] = True  # 有激活（或正在测试）的档案即启用；无档案时沿用 yaml 的 enabled
    merged = SECTION_TYPES[role].model_validate({**current.model_dump(), **fields})
    if role in _STUDIO_ROLES:
        return config.model_copy(update={"studio": config.studio.model_copy(update={role: merged})})
    if role == "agent":
        return config.model_copy(update={"agent": config.agent.model_copy(update={"llm": merged})})
    return config.model_copy(update={role: merged})


def _animator_follows_agent(config: Config, active: dict) -> Config:
    """yaml 未写 studio.animator（复制自 agent.llm）且没有激活动画生成档案时，跟随激活的智能助手档案。"""
    agent = active.get("agent")
    animator = config.studio.animator
    if agent is None or "animator" in active or not animator._inherited:
        return config
    llm = config.agent.llm
    merged = animator.model_copy(update={**{key: getattr(llm, key) for key in ANIMATOR_INHERITED},
                                         "api_key": llm.api_key})
    merged._inherited = True
    return config.model_copy(update={"studio": config.studio.model_copy(update={"animator": merged})})


def shared_analyzer_profile(store: Store, model: str) -> dict | None:
    """多模态理解里与 ``model`` 同名（不区分大小写）且带密钥的档案：激活的档案优先，其次最近更新的。"""
    if not model:
        return None
    pointer = get_setting(store, "active.profile.analyzer")
    rows = store.rows("SELECT * FROM model_profiles WHERE role='analyzer' AND api_key IS NOT NULL AND api_key<>'' "
                      "ORDER BY updated_at DESC, id DESC")
    matches = [{**row, "config": json.loads(row["config"])} for row in rows
               if str(json.loads(row["config"]).get("model") or "").lower() == model.lower()]
    matches.sort(key=lambda row: str(row["id"]) != pointer)
    return matches[0] if matches else None


def shared_analyzer_key(store: Store, model: str) -> str | None:
    found = shared_analyzer_profile(store, model)
    return found["api_key"] if found else None


def animator_shares_analyzer_key(config: Config, store: Store) -> Config:
    """动画生成没有自己的密钥（档案未填且环境变量未设）时，复用多模态理解里同名模型（如 GLM-5.3-Flash）档案的
    密钥与 base_url：密钥与端点成对（例如智谱 GLM Coding Plan 的密钥只能用于 /api/coding/paas/v4，
    用于通用端点 /api/paas/v4 会按余额计费而报 429「余额不足」），只借密钥会连错端点。"""
    animator = config.studio.animator
    if animator.api_key or os.environ.get(animator.api_key_env, ""):
        return config
    found = shared_analyzer_profile(store, animator.model)
    if not found:
        return config
    update = {"api_key": found["api_key"]}
    if found["config"].get("base_url"):
        update["base_url"] = found["config"]["base_url"]
    merged = animator.model_copy(update=update)
    merged._inherited = animator._inherited
    return config.model_copy(update={"studio": config.studio.model_copy(update={"animator": merged})})


def apply_overrides(config: Config, store: Store) -> Config:
    """按激活档案与业务键合成生效配置；返回新对象，无覆盖时返回原对象。"""
    if not config.runtime_overrides:
        return config
    active = {}
    for role in ROLES:
        profile = _active_profile(store, role)
        if profile is not None:
            active[role] = profile
            config = merge_profile(config, role, profile)
    config = _animator_follows_agent(config, active)
    config = animator_shares_analyzer_key(config, store)

    raw_search = get_setting(store, "override.search")
    if raw_search:
        try:
            partial = json.loads(raw_search)
        except json.JSONDecodeError as exc:
            raise ValueError("override.search 不是有效 JSON") from exc
        allowed = {"weights", "channel_weights", "rrf_k", "candidates"}
        unknown = set(partial) - allowed
        if unknown:
            raise ValueError(f"检索调参不支持字段 {sorted(unknown)}；仅允许 {sorted(allowed)}")
        config = config.model_copy(
            update={"search": SearchConfig.model_validate({**config.search.model_dump(), **partial})}
        )

    gpu_lock = get_setting(store, "override.enforce_gpu_lock")
    if gpu_lock is not None:
        value = json.loads(gpu_lock)
        if not isinstance(value, bool):
            raise ValueError("enforce_gpu_lock 必须是布尔值")
        config = config.model_copy(update={"enforce_gpu_lock": value})

    voice = get_setting(store, "override.studio.voice")
    if voice:
        config = config.model_copy(
            update={
                "studio": config.studio.model_copy(
                    update={"tts": config.studio.tts.model_copy(update={"voice": voice})}
                )
            }
        )
    return config


WRITER_MIGRATION_KEY = "migration.writer_profile"


def ensure_writer_profile(store: Store) -> dict | None:
    """一次性迁移：新增「文案创作」角色时，把激活的「文本拆分镜」档案复制一份并激活。

    只在从未迁移过、且还没有任何文案创作档案时执行；完成后写标记，之后用户删除或
    改动文案档案都不会被重新覆盖。没有激活的拆分镜档案时，文案配置沿用 yaml
    （未配置 studio.writer 时即复制 studio.generator）。
    """
    if get_setting(store, WRITER_MIGRATION_KEY):
        return None
    created = None
    source = _active_profile(store, "generator")
    if source is not None and not list_profiles(store, "writer"):
        profile = save_profile(
            store,
            role="writer",
            name=source["name"],
            provider=source["provider"],
            config={k: v for k, v in source["config"].items() if k in PROFILE_FIELDS["writer"]},
            api_key=source.get("api_key") or None,
        )
        activate_profile(store, profile["id"])
        created = profile
    put_setting(store, WRITER_MIGRATION_KEY, "1")
    return created


DEFAULT_PROFILES_KEY = "migration.default_profiles"


def ensure_default_profiles(store: Store) -> list[dict]:
    return seed_default_profiles(store)


def seed_default_profiles(store: Store) -> list[dict]:
    """一次性：为动画/视频/图片/音乐生成建默认档案卡（MiniMax 或 GLM 默认激活，本地 ComfyUI 备用）。

    已有档案的角色不建；角色没有激活档案时才激活第一张。完成后写标记，之后删除或改动不会被重新覆盖。"""
    if get_setting(store, DEFAULT_PROFILES_KEY):
        return []
    created = []
    for role, entries in DEFAULT_PROFILES.items():
        if list_profiles(store, role):
            continue
        for index, (name, provider, config) in enumerate(entries):
            profile = save_profile(store, role=role, name=name, provider=provider, config=dict(config))
            if index == 0 and get_setting(store, f"active.profile.{role}") is None:
                activate_profile(store, profile["id"])
            created.append(profile)
    put_setting(store, DEFAULT_PROFILES_KEY, "1")
    return created


def apply_runtime_overrides(config: Config) -> Config:
    """CLI 入口：data_dir 已有元数据库时合并覆盖层，否则原样返回。"""
    if not config.runtime_overrides or not (config.data_dir / "metadata.sqlite3").is_file():
        return config
    store = Store(config.data_dir)
    ensure_writer_profile(store)
    ensure_default_profiles(store)
    return apply_overrides(config, store)


class RuntimeConfig:
    """Web 进程的生效配置提供者：每次 current() 重新合并，保证读到最新设置。"""

    def __init__(self, base: Config, store: Store):
        self.base = base
        self.store = store
        if base.runtime_overrides:
            ensure_writer_profile(store)
            ensure_default_profiles(store)

    def current(self) -> Config:
        return apply_overrides(self.base, self.store)
