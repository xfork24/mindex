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
import sqlite3

from mindex.shared.config import (
    AnalyzerConfig,
    ASRConfig,
    Config,
    EmbeddingConfig,
    GeneratorConfig,
    PersonConfig,
    SearchConfig,
    SpeakerConfig,
)
from mindex.shared.storage import Store

ROLES = ("analyzer", "generator", "embedding", "asr", "tts", "person")
ROLE_LABELS = {
    "person": "人物识别",
    "analyzer": "多模态理解",
    "generator": "文本拆分镜",
    "embedding": "多模态嵌入",
    "asr": "语音识别",
    "tts": "语音合成",
}

# 各角色允许 profile 覆盖的字段；未列字段永远承袭 yaml 原值。
PROFILE_FIELDS = {
    "person": {
        "backend", "base_url", "model", "revision", "timeout", "api_key_env", "expected_space",
        "fps", "frame_interval", "face_threshold", "face_margin", "min_face_quality", "min_reference_quality", "body_threshold",
    },
    "analyzer": {
        "backend", "base_url", "model", "revision", "timeout", "api_key_env", "llama_template",
        "response_retries", "initial_output_tokens", "max_output_tokens", "thinking_mode",
    },
    "generator": {"backend", "base_url", "model", "revision", "timeout", "api_key_env", "llama_template", "max_tokens"},
    "embedding": {"backend", "base_url", "model", "revision", "timeout", "api_key_env", "quantization", "dimensions"},
    "asr": {"backend", "base_url", "model", "revision", "timeout", "api_key_env", "enabled"},
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
}
SECTION_TYPES = {
    "person": PersonConfig,
    "analyzer": AnalyzerConfig,
    "generator": GeneratorConfig,
    "embedding": EmbeddingConfig,
    "asr": ASRConfig,
    "tts": SpeakerConfig,
}
_STUDIO_ROLES = {"generator", "tts"}

PROVIDERS = ("custom", "self", "zhipu", "deepseek", "aliyun", "tencent", "minimax")
PROVIDER_LABELS = {
    "custom": "自定义",
    "self": "自建服务",
    "zhipu": "智谱",
    "deepseek": "DeepSeek",
    "aliyun": "阿里云百炼",
    "tencent": "腾讯云",
    "minimax": "MiniMax",
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
    },
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
    if role == "generator":
        return config.studio.generator
    if role == "tts":
        return config.studio.tts
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
    fields["api_key"] = profile.get("api_key") or ""
    merged = SECTION_TYPES[role].model_validate({**current.model_dump(), **fields})
    if role in _STUDIO_ROLES:
        return config.model_copy(update={"studio": config.studio.model_copy(update={role: merged})})
    return config.model_copy(update={role: merged})


def apply_overrides(config: Config, store: Store) -> Config:
    """按激活档案与业务键合成生效配置；返回新对象，无覆盖时返回原对象。"""
    if not config.runtime_overrides:
        return config
    for role in ROLES:
        profile = _active_profile(store, role)
        if profile is not None:
            config = merge_profile(config, role, profile)

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


def apply_runtime_overrides(config: Config) -> Config:
    """CLI 入口：data_dir 已有元数据库时合并覆盖层，否则原样返回。"""
    if not config.runtime_overrides or not (config.data_dir / "metadata.sqlite3").is_file():
        return config
    return apply_overrides(config, Store(config.data_dir))


class RuntimeConfig:
    """Web 进程的生效配置提供者：每次 current() 重新合并，保证读到最新设置。"""

    def __init__(self, base: Config, store: Store):
        self.base = base
        self.store = store

    def current(self) -> Config:
        return apply_overrides(self.base, self.store)
