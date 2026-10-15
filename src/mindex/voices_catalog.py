"""TTS 音色目录：按当前合成后端聚合系统音色、本地音色库与克隆能力。

- 本地 openai 后端（qwentts.cpp tts-server）：静态 CustomVoice 音色表 + GET /audio/voices
  动态列表合并；克隆音色注册存于服务进程内存，服务重启后靠本模块在目录加载时
  用存档样本惰性重注册自愈（ref_text 存于 voices.notes）。
- minimax 后端：POST /get_voice（voice_type=all）实时查询系统/复刻/生成音色，
  系统音色按 voice_id 前缀（如 Chinese (Mandarin)_）分组。
- 任何上游失败都降级为 error 提示 + 静态表/本地库，不让设置页 500。
"""

from __future__ import annotations

import logging

from . import adapters
from .config import Config
from .runtime_settings import get_setting, list_voices
from .storage import Store
from .voice_clone import qwentts_register

logger = logging.getLogger(__name__)

# Qwen3-TTS CustomVoice 内置音色（服务端 /audio/voices 返回小写 id；描述来自官方音色表）。
QWEN3_TTS_CUSTOMVOICE: list[dict] = [
    {"voice_id": "vivian", "name": "Vivian", "description": "明快、略带锋芒的年轻女声", "language": "中文"},
    {"voice_id": "serena", "name": "Serena", "description": "温暖柔和的年轻女声", "language": "中文"},
    {"voice_id": "uncle_fu", "name": "Uncle_Fu", "description": "沉稳低沉的醇厚男声", "language": "中文"},
    {"voice_id": "dylan", "name": "Dylan", "description": "清亮自然的北京男声", "language": "中文（京腔）"},
    {"voice_id": "eric", "name": "Eric", "description": "活泼微哑、带亮度的成都男声", "language": "中文（四川话）"},
    {"voice_id": "ryan", "name": "Ryan", "description": "节奏感强的活力男声", "language": "英语"},
    {"voice_id": "aiden", "name": "Aiden", "description": "明朗的美式男声", "language": "英语"},
    {"voice_id": "ono_anna", "name": "Ono_Anna", "description": "轻快俏皮的日语女声", "language": "日语"},
    {"voice_id": "sohee", "name": "Sohee", "description": "情感丰富的温暖韩语女声", "language": "韩语"},
]

_STATIC_LABELS = {item["voice_id"]: item for item in QWEN3_TTS_CUSTOMVOICE}

# voices 表 provider 与 TTS 后端的对应（决定哪些库内音色当前可用）。
_PROVIDER_FOR_BACKEND = {
    "openai": {"self", "openai"},
    "minimax": {"minimax"},
    "dashscope": {"dashscope"},
}


def _label_for(voice_id: str) -> str:
    item = _STATIC_LABELS.get(voice_id) or _STATIC_LABELS.get(voice_id.lower())
    if item:
        return f"{item['name']} · {item['description']}（{item['language']}）"
    return voice_id


def _live_voices(tts) -> tuple[list[str], str | None]:
    try:
        payload = adapters.request("GET", tts.base_url.rstrip("/") + "/audio/voices", timeout=tts.timeout)
    except Exception as exc:  # noqa: BLE001 — 目录拉取失败必须降级而不是 500
        return [], f"音色列表拉取失败：{str(exc)[:200]}"
    voices = payload.get("voices") if isinstance(payload, dict) else None
    if not isinstance(voices, list):
        return [], "音色列表响应格式不符合约定"
    ids = []
    for item in voices:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and isinstance(item.get("voice_id") or item.get("name"), str):
            ids.append(item.get("voice_id") or item.get("name"))
    return ids, None


def _heal_local_clones(store: Store, tts, live: list[str]) -> list[str]:
    """qwentts.cpp 克隆注册存进程内存；把存档样本里服务端缺失的克隆重新注册。"""
    healed: list[str] = []
    for row in list_voices(store):
        if row["provider"] not in {"self", "openai"} or row["kind"] != "clone":
            continue
        if row["voice_id"] in live:
            continue
        sample = store.root / row["sample_path"] if row["sample_path"] else None
        if sample is None or not sample.is_file():
            continue
        try:
            qwentts_register(tts.base_url, sample, row["voice_id"], row["notes"] or "")
            healed.append(row["voice_id"])
        except Exception as exc:  # noqa: BLE001 — 自愈失败不阻塞目录返回，合成时会有明确报错
            logger.warning("克隆音色 %s 重注册失败：%s", row["voice_id"], exc)
            continue
    return healed


def _minimax_groups(tts) -> tuple[list[dict], list[dict], str | None]:
    try:
        payload = adapters.request(
            "POST", tts.base_url.rstrip("/") + "/get_voice", timeout=tts.timeout,
            headers=tts.headers(), json={"voice_type": "all"},
        )
    except Exception as exc:  # noqa: BLE001
        return [], [], f"MiniMax 音色查询失败：{str(exc)[:200]}"
    if (payload.get("base_resp") or {}).get("status_code", 0) != 0:
        return [], [], f"MiniMax 音色查询失败：{payload.get('base_resp')}"

    grouped: dict[str, list[dict]] = {}
    for item in payload.get("system_voice") or []:
        voice_id = item.get("voice_id")
        if not isinstance(voice_id, str):
            continue
        name = item.get("voice_name") or voice_id
        descriptions = item.get("description") or []
        hint = descriptions[0] if descriptions and isinstance(descriptions[0], str) else ""
        prefix = voice_id.split("_", 1)[0] if "_" in voice_id and "(" in voice_id.split("_", 1)[0] else None
        key = prefix or "经典音色"
        grouped.setdefault(key, []).append(
            {"voice_id": voice_id, "label": f"{name}（{voice_id}）" if hint == "" else f"{name} · {hint}（{voice_id}）"}
        )
    groups = [{"label": label, "voices": entries} for label, entries in grouped.items()]

    library: list[dict] = []
    for field, kind in (("voice_cloning", "clone"), ("voice_generation", "generated")):
        for item in payload.get(field) or []:
            voice_id = item.get("voice_id")
            if isinstance(voice_id, str):
                library.append({"voice_id": voice_id, "name": voice_id, "kind": kind, "sample_present": False})
    return groups, library, None


def _local_library(store: Store, backend: str) -> list[dict]:
    allowed = _PROVIDER_FOR_BACKEND.get(backend, set())
    entries = []
    for row in list_voices(store):
        if row["provider"] not in allowed:
            continue
        entries.append(
            {
                "voice_id": row["voice_id"],
                "name": row["name"],
                "kind": row["kind"],
                "sample_present": bool(
                    row["sample_path"] and (store.root / row["sample_path"]).is_file()
                ),
            }
        )
    return entries


def voice_kind(model: str, name: str = "") -> str:
    """判定 TTS 档案的音色形态：VoiceClone 型（Base 模型，无预设音色）必须用音色库克隆音色。

    本地 qwentts 档案在 model 字段带 Qwen3-TTS-VoiceClone/CustomVoice 标识；
    MiniMax 两种形态共用 speech-2.8-hd，靠档案名后缀区分。
    """
    normalized = f"{model} {name}".lower().replace("-", "").replace("_", "").replace(" ", "")
    return "clone" if "voiceclone" in normalized else "system"


def fetch_catalog(config: Config, store: Store) -> dict:
    tts = config.studio.tts
    backend = tts.backend
    groups: list[dict] = []
    remote_library: list[dict] = []
    error: str | None = None
    clone = {"supported": False, "mode": None, "ref_text": False}

    if backend == "openai":
        clone = {"supported": True, "mode": "upload", "ref_text": True}
        live, live_error = _live_voices(tts)
        error = live_error
        healed = _heal_local_clones(store, tts, live) if live_error is None else []
        merged_ids = list(dict.fromkeys([*live, *healed, *[item["voice_id"] for item in QWEN3_TTS_CUSTOMVOICE]]))
        voices = [{"voice_id": identifier, "label": _label_for(identifier)} for identifier in merged_ids]
        groups = [{"label": "系统音色 · Qwen3-TTS", "voices": voices}]
    elif backend == "minimax":
        clone = {"supported": True, "mode": "upload", "ref_text": False}
        groups, remote_library, error = _minimax_groups(tts)

    library_by_id = {entry["voice_id"]: entry for entry in _local_library(store, backend)}
    for entry in remote_library:
        library_by_id.setdefault(entry["voice_id"], entry)

    return {
        "backend": backend,
        "clone": clone,
        "groups": groups,
        "library": list(library_by_id.values()),
        "default_voice": get_setting(store, "override.studio.voice"),
        "error": error,
    }
