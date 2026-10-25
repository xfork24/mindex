"""设置页 API：模型服务配置档案、业务调参、连接测试与热生效状态。

GET /api/settings 返回的永远是「当前生效」视图——激活档案 + 掩码密钥 +
各角色生效值 + 嵌入身份与活动索引的匹配状态，供前端直接呈现。
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
import time
import wave
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from mindex.models import adapters
from mindex.models.adapters import (
    make_animator,
    make_embedder,
    make_image_generator,
    make_music_generator,
    make_speaker,
    make_transcriber,
    make_video_generator,
)
from mindex.models.qwen import analysis_options
from mindex.settings.runtime_settings import (
    PROFILE_FIELDS,
    PROVIDER_LABELS,
    PROVIDER_PRESETS,
    ROLE_LABELS,
    ROLES,
    RuntimeConfig,
    _section,
    activate_profile,
    animator_shares_analyzer_key,
    delete_profile,
    delete_voice,
    get_setting,
    get_voice,
    list_profiles,
    list_voices,
    mask_key,
    merge_profile,
    put_setting,
    save_profile,
    save_voice,
    shared_analyzer_key,
)
from mindex.settings.voice_clone import aliyun_enroll, minimax_clone, qwentts_register
from mindex.settings.voices_catalog import fetch_catalog, voice_kind
from mindex.shared.comfyui import workflow_hash
from mindex.shared.config import Config, SearchConfig, SpeakerConfig, StrictModel
from mindex.shared.storage import Store
from mindex.shared.uploads import save_upload

log = logging.getLogger(__name__)

_SETTING_KEYS = ("override.search", "override.studio.voice", "override.enforce_gpu_lock")
_CLONE_SAMPLE_SUFFIXES = {".wav", ".mp3", ".m4a"}
MAX_VOICE_SAMPLE_BYTES = 50 * 1024 * 1024  # 声音样本上传上限（50 MiB）
TEST_DETAIL_MAX_CHARS = 500
_SECRET_WORDS = ("key", "token", "secret", "password", "passwd", "signature", "auth", "credential")
_REDACTED = "<已隐藏>"
_URL_QUERY = re.compile(r"((?:https?|wss?)://[^\s?#'\"<>]+)[?#][^\s'\"<>]*", re.IGNORECASE)
_BEARER = re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"([\w.-]*(?:" + "|".join(_SECRET_WORDS) + r")[\w.-]*)(['\"]?\s*[=:]\s*['\"]?)[^\s&'\",;}<>]+",
    re.IGNORECASE,
)
_KEY_LIKE = re.compile(r"\b(sk|pk|ak)-[A-Za-z0-9_-]{8,}")


def redact_error(text: str, known_secrets: list[str] | tuple[str, ...] = ()) -> str:
    """去掉错误文本中的 URL 查询串、Bearer 凭证、``key=...`` 形式的值以及已知密钥。"""
    for secret in sorted({value for value in known_secrets if len(value) >= 4}, key=len, reverse=True):
        text = text.replace(secret, _REDACTED)
    text = _URL_QUERY.sub(lambda m: f"{m.group(1)}?{_REDACTED}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {_REDACTED}", text)
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
    return _KEY_LIKE.sub(lambda m: f"{m.group(1)}-{_REDACTED}", text)


class ProfileBody(StrictModel):
    role: str
    name: str = Field(min_length=1, max_length=60)
    provider: str
    config: dict
    api_key: str | None = Field(default=None, max_length=4096)


class VoiceBody(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    provider: str
    voice_id: str = Field(min_length=1, max_length=200)
    kind: str = "preset"
    notes: str = Field(default="", max_length=500)


class SearchTuning(StrictModel):
    channel_weights: dict[str, float] | None = None
    weights: dict[str, float] | None = None
    rrf_k: int | None = Field(default=None, ge=1)
    candidates: int | None = Field(default=None, ge=1, le=1000)


class BusinessBody(StrictModel):
    enforce_gpu_lock: bool | None = Field(default=None, strict=True)
    search: SearchTuning | None = None
    default_voice_id: int | None = None
    default_voice: str | None = None
    clear_default_voice: bool = False


def _effective(section, role: str) -> dict:
    dump = section.model_dump()
    info = {
        "backend": dump.get("backend", "openai"),
        "base_url": dump["base_url"],
        "model": dump["model"],
        "revision": dump["revision"],
        "timeout": dump["timeout"],
        "api_key": mask_key(getattr(section, "api_key", "") or None),
        "api_key_env": dump.get("api_key_env", ""),
        "env_key_set": bool(os.environ.get(dump.get("api_key_env", ""), "")),
    }
    extras = {
        "analyzer": ("response_retries", "initial_output_tokens", "max_output_tokens", "thinking_mode"),
        "person": (
            "expected_space", "fps", "frame_interval", "face_threshold", "face_margin", "min_face_quality",
            "min_reference_quality", "body_threshold",
        ),
        "embedding": ("dimensions", "quantization"),
        "asr": ("enabled", "max_tokens"),
        "tts": ("voice", "response_format"),
        "video_generator": ("enabled", "durations", "fps", "poll_interval", "max_wait"),
        "animator": ("enabled", "max_tokens", "retries", "max_capture_seconds", "max_html_kb"),
        "image_generator": ("enabled", "poll_interval", "max_wait"),
        "music_generator": ("enabled", "cover_model", "poll_interval", "max_wait", "num_inference_steps", "max_seconds"),
        "generator": ("max_tokens",),
        "writer": ("max_tokens",),
        "agent": ("max_tokens", "vision"),
    }
    for key in extras.get(role, ()):
        info[key] = dump.get(key)
    if role in ("video_generator", "image_generator", "music_generator"):
        # 工作流全文不出现在概览里：只报告是否已导入与摘要。
        info["comfyui_workflow"] = workflow_hash(dump.get("comfyui_workflow"))
        info["comfyui_mapping"] = dump.get("comfyui_mapping")
    return info


def _presets(current: Config) -> dict:
    presets = {
        provider: {role: {"config": entry["config"], "note": entry.get("note", "")}
                   for role, entry in roles.items()}
        for provider, roles in PROVIDER_PRESETS.items()
    }
    # 「自建/当前」预设 = 当前生效配置，便于复制一份再改。
    presets["self"] = {
        role: {
            "config": {
                key: value
                for key, value in _section(current, role).model_dump().items()
                if key in PROFILE_FIELDS[role]
            },
            "note": "当前生效配置",
        }
        for role in ROLES
    }
    return presets


def _silence_wav(path: Path, seconds: float = 0.5) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * seconds))


ECHO_TOOL = {
    "name": "echo",
    "description": "连接测试工具：原样返回 text。",
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}


def _probe_agent(config: Config, *, transport=None) -> dict:
    """智能助手必须支持工具调用：要求模型调用 echo 工具，并核对调用名与参数可解析。"""
    import asyncio

    from mindex.agent.llm.factory import make_agent_llm
    from mindex.agent.llm.types import LlmRequest, collect, parse_arguments

    request = LlmRequest(
        system="你在做连接测试。必须调用 echo 工具，参数 text 为 ok，不要输出其他内容。",
        messages=[{"id": "probe", "role": "user", "source": {"kind": "user"},
                   "content": [{"type": "text", "text": "请调用 echo 工具，text=ok"}]}],
        tools=[ECHO_TOOL],
        max_tokens=min(config.agent.llm.max_tokens, 512),
        temperature=0,
    )
    result = asyncio.run(collect(make_agent_llm(config, transport=transport), request))
    calls = [b for b in result.content if b["type"] == "tool-call"]
    text = "".join(b.get("text", "") for b in result.content if b["type"] == "text")
    ok = bool(calls) and calls[0]["name"] == "echo"
    if ok:
        try:
            parse_arguments(calls[0]["arguments"])
        except ValueError:
            ok = False
    return {
        "model": config.agent.llm.model,
        "backend": config.agent.llm.backend,
        "tool_calling": ok,
        "reply": "工具调用可用" if ok else f"模型未按要求调用工具（{text[:60] or '无文本输出'}）",
    }


def _probe(config: Config, role: str, *, store: Store | None = None, profile_name: str = "") -> dict:
    """对单个角色做一次最小真实调用，返回可呈现的探测结果。"""
    if role == "person":
        from mindex.persons.person_processing import PersonClient
        return {"ready": True, "space": PersonClient(config).identity()}
    if role == "agent":
        return _probe_agent(config)
    if role == "video_generator":
        # 只做鉴权/可达性探测：绝不提交生成任务（线上生成按次计费）。
        return make_video_generator(config).health()
    if role == "image_generator":
        # 只做鉴权/可达性探测：绝不生成图片。
        return make_image_generator(config).health()
    if role == "music_generator":
        # 只做鉴权/可达性探测：绝不生成音乐。
        return make_music_generator(config).health()
    if role == "animator":
        # 一次最小对话（不生成动画）：验证地址、模型名与密钥。
        return make_animator(config).ping()
    if role in {"analyzer", "generator", "writer"}:
        service = (
            config.analyzer if role == "analyzer"
            else config.studio.writer if role == "writer"
            else config.studio.generator
        )
        extra = (
            analysis_options(service)
            if role == "analyzer" else
            {"chat_template_kwargs": {"enable_thinking": False}}
            if getattr(service, "llama_template", False)
            else {}
        )
        payload = adapters.request(
            "POST",
            service.base_url.rstrip("/") + "/chat/completions",
            timeout=service.timeout,
            headers=service.headers(),
            # 推理模型（如 MiniMax-M3.1）先消耗思考 token，预算太小会导致正文永远为空；
            # 但这里必须用档案实际配置的 max_tokens，否则参数超限这类拒绝在测试里看不见。
            json={"model": service.model,
                  "max_tokens": getattr(service, "max_tokens", 512),
                  "messages": [{"role": "user", "content": "连接测试，请回复：ok"}], **extra},
        )
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        reply = message.get("content")
        if not reply:
            finish = choice.get("finish_reason")
            reasoning = len(message.get("reasoning_content") or "")
            reply = f"（未返回正文；finish_reason={finish}，推理 {reasoning} 字）"
        return {"model": service.model, "reply": str(reply)[:80]}

    if role == "embedding":
        embedder = make_embedder(config)
        identity = embedder.identity()
        vector = embedder.text(["连接测试"])[0]
        # schema 2 的空间契约只有内容摘要、没有 model 键，用配置里的模型名兜底。
        model = identity.get("model") or config.embedding.model
        return {"model": model, "dims": len(vector)}

    if role == "asr":
        transcriber = make_transcriber(config)
        with tempfile.TemporaryDirectory(prefix="settings-asr-") as directory:
            audio = Path(directory) / "silence.wav"
            _silence_wav(audio)
            if hasattr(transcriber, "raw"):
                text = transcriber.raw(audio)["text"]
            else:
                transcriber.transcribe(audio)
                text = ""
        return {"model": config.asr.model, "reply": text[:80] or "（静音样本，无识别结果属正常）"}

    tts = config.studio.tts
    voice = tts.voice
    kind = voice_kind(tts.model, profile_name)
    candidates = None
    if tts.backend == "openai" and "qwen3" in tts.model.lower():
        from mindex.settings.voices_catalog import _heal_local_clones, _live_voices

        candidates, error = _live_voices(tts)
        if error:
            raise adapters.ModelError(error)
        if store is not None and kind == "clone":
            candidates.extend(_heal_local_clones(store, tts, candidates))
    elif tts.backend == "minimax" and store is not None:
        catalog = fetch_catalog(config, store)
        if catalog["error"]:
            raise adapters.ModelError(catalog["error"])
        entries = catalog["library"] if kind == "clone" else [
            entry for group in catalog["groups"] for entry in group["voices"]
        ]
        candidates = [entry["voice_id"] for entry in entries]
    if candidates is not None:
        known = {candidate.lower(): candidate for candidate in candidates}
        if not candidates:
            return {
                "model": tts.model,
                "ready": False,
                "reply": "服务连接正常；目标模型暂无可用音色，未执行合成测试。"
                + ("请先添加对应服务的克隆音色。" if kind == "clone" else ""),
            }
        # Probe the target model's catalog, never an unrelated provider's voice.
        voice = known.get(voice.lower(), candidates[0])
    speaker = make_speaker(config)
    audio_bytes = speaker.speak("测试", voice=voice)
    return {"model": tts.model, "voice": voice, "ready": True, "bytes": len(audio_bytes)}


def settings_router(runtime: RuntimeConfig, store: Store) -> APIRouter:
    router = APIRouter(prefix="/api/settings", tags=["设置"])

    def _with_sample(voice: dict | None) -> dict | None:
        if voice is None:
            return None
        return {
            **voice,
            "sample_present": bool(
                voice.get("sample_path") and (store.root / voice["sample_path"]).is_file()
            ),
        }

    @router.get("/embedding-status")
    def get_embedding_status():
        """独立执行可能读取大模型文件、访问远端的身份校验，不阻塞配置展示。"""
        current = runtime.current()
        active = store.active_index()
        if active is None:
            embedding_status = {"active_index_id": None, "matches_index": None, "identity": None}
        else:
            try:
                identity = make_embedder(current).identity()
                embedding_status = {
                    "active_index_id": active["id"],
                    "matches_index": identity == active["spec"]["space"],
                    "identity": identity,
                }
            except Exception as exc:  # noqa: BLE001 — 身份探测失败必须如实呈现而非 500
                embedding_status = {
                    "active_index_id": active["id"],
                    "matches_index": False,
                    "identity": None,
                    "error": str(exc)[:300],
                }

        return embedding_status

    @router.get("")
    def get_settings(probe_embedding: bool = True):
        current = runtime.current()
        by_role: dict[str, list[dict]] = {role: [] for role in ROLES}
        for profile in list_profiles(store):
            by_role[profile["role"]].append(profile)
        roles_payload = []
        for role in ROLES:
            pointer = get_setting(store, f"active.profile.{role}")
            roles_payload.append(
                {
                    "role": role,
                    "label": ROLE_LABELS[role],
                    "active_profile_id": int(pointer) if pointer else None,
                    "effective": _effective(_section(current, role), role),
                    "profiles": by_role[role],
                }
            )
        # 动画生成的密钥复用自多模态理解的同名模型档案时，在概览里注明来源（仍只给掩码）。
        animator = next(item for item in roles_payload if item["role"] == "animator")
        own = next((p for p in animator["profiles"] if p["id"] == animator["active_profile_id"]), None)
        if (current.studio.animator.api_key and not (own and own["api_key"]["has_key"])
                and current.studio.animator.api_key == shared_analyzer_key(store, current.studio.animator.model)):
            animator["effective"]["key_shared_from"] = "analyzer"

        embedding_status = get_embedding_status() if probe_embedding else None

        voices = [_with_sample(voice) for voice in list_voices(store)]

        return {
            "roles": roles_payload,
            "providers": PROVIDER_LABELS,
            "presets": _presets(current),
            "embedding_status": embedding_status,
            "voices": voices,
            "business": {
                "enforce_gpu_lock": current.enforce_gpu_lock,
                "search": {
                    "weights": current.search.weights,
                    "channel_weights": current.search.channel_weights,
                    "rrf_k": current.search.rrf_k,
                    "candidates": current.search.candidates,
                },
                "default_voice": get_setting(store, "override.studio.voice"),
                "tts_backend": current.studio.tts.backend,
            },
        }

    @router.post("/profiles", status_code=201)
    def create_profile(body: ProfileBody):
        return save_profile(
            store,
            role=body.role,
            name=body.name,
            provider=body.provider,
            config=body.config,
            api_key=body.api_key,
        )

    @router.put("/profiles/{profile_id}")
    def update_profile(profile_id: int, body: ProfileBody):
        return save_profile(
            store,
            role=body.role,
            name=body.name,
            provider=body.provider,
            config=body.config,
            api_key=body.api_key,
            profile_id=profile_id,
        )

    @router.delete("/profiles/{profile_id}", status_code=204)
    def remove_profile(profile_id: int):
        delete_profile(store, profile_id)

    @router.post("/profiles/{profile_id}/activate")
    def activate(profile_id: int):
        result = activate_profile(store, profile_id)
        warning = None
        if result["role"] == "embedding":
            active = store.active_index()
            if active is not None:
                try:
                    identity = make_embedder(runtime.current()).identity()
                    mismatch = identity != active["spec"]["space"]
                except Exception as exc:  # noqa: BLE001 — 探测失败也要给出可读提示
                    mismatch, identity = True, None
                    active = {**active, "probe_error": str(exc)[:300]}
                if mismatch:
                    warning = {
                        "active_index_id": active["id"],
                        "message": "当前嵌入身份与已激活索引不一致，检索将返回未就绪；"
                        "请运行 mindex index --activate 构建并发布新索引后再检索",
                        **({"probe_error": active["probe_error"]} if "probe_error" in active else {}),
                    }
        return {**result, "index_warning": warning}

    @router.post("/profiles/{profile_id}/test")
    def test_connection(profile_id: int):
        row = store.one("SELECT * FROM model_profiles WHERE id=?", (profile_id,))
        if row is None:
            raise HTTPException(404, "配置档案不存在")
        profile = {**row, "config": json.loads(row["config"])}
        started = time.monotonic()
        try:
            config = merge_profile(runtime.base, profile["role"], profile)
            if profile["role"] == "animator":
                config = animator_shares_analyzer_key(config, store)
            detail = _probe(config, profile["role"], store=store, profile_name=profile["name"])
            if detail.get("tool_calling") is False:
                return {"ok": False, "latency_ms": round((time.monotonic() - started) * 1000),
                        "detail": detail["reply"], **detail}
            return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000), **detail}
        except Exception as exc:  # 测试结果要回报给页面而不是 500
            # 完整错误只写服务端日志；页面拿到的是脱敏后的摘要。
            log.warning("配置档案 %s 连接测试失败", profile_id, exc_info=True)
            secrets_in_profile = [str(profile.get("api_key") or "")] + [
                str(value) for key, value in profile["config"].items()
                if isinstance(value, str) and value and any(word in key.lower() for word in _SECRET_WORDS)
            ]
            return {
                "ok": False,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "detail": redact_error(str(exc), secrets_in_profile)[:TEST_DETAIL_MAX_CHARS],
            }

    @router.put("/business")
    def put_business(body: BusinessBody):
        current = runtime.current()
        if body.enforce_gpu_lock is not None:
            put_setting(store, "override.enforce_gpu_lock", json.dumps(body.enforce_gpu_lock))
        if body.search is not None:
            partial = {k: v for k, v in body.search.model_dump(exclude_none=True).items() if v is not None}
            if partial:
                merged = SearchConfig.model_validate({**current.search.model_dump(), **partial})
                put_setting(
                    store,
                    "override.search",
                    json.dumps(
                        {
                            "weights": merged.weights,
                            "channel_weights": merged.channel_weights,
                            "rrf_k": merged.rrf_k,
                            "candidates": merged.candidates,
                        },
                        ensure_ascii=False,
                    ),
                )
        if body.clear_default_voice:
            store.execute("DELETE FROM settings WHERE key='override.studio.voice'")
        elif body.default_voice_id is not None:
            voice = get_voice(store, body.default_voice_id)
            if voice is None:
                raise HTTPException(404, "音色不存在")
            required_backend = {"minimax": "minimax", "dashscope": "dashscope"}.get(voice["provider"])
            if required_backend and current.studio.tts.backend != required_backend:
                raise HTTPException(
                    422,
                    f"音色厂商 {voice['provider']} 与当前语音合成后端 "
                    f"{current.studio.tts.backend} 不匹配，请先激活对应档案",
                )
            put_setting(store, "override.studio.voice", voice["voice_id"])
        elif body.default_voice is not None:
            literal = body.default_voice.strip()
            if not literal:
                raise HTTPException(422, "默认音色不能为空；清除请使用 clear_default_voice")
            row = store.one("SELECT * FROM voices WHERE voice_id=?", (literal,))
            if row is not None:
                required_backend = {"minimax": "minimax", "dashscope": "dashscope"}.get(row["provider"])
                if required_backend and current.studio.tts.backend != required_backend:
                    raise HTTPException(
                        422,
                        f"音色厂商 {row['provider']} 与当前语音合成后端 "
                        f"{current.studio.tts.backend} 不匹配，请先激活对应档案",
                    )
            else:
                catalog = fetch_catalog(current, store)
                known = {
                    voice["voice_id"] for group in catalog["groups"] for voice in group["voices"]
                } | {voice["voice_id"] for voice in catalog["library"]}
                if literal not in known:
                    raise HTTPException(422, f"未知音色 {literal}；不在当前后端目录或音色库中")
            put_setting(store, "override.studio.voice", literal)
        return get_settings()["business"]

    @router.delete("/business", status_code=204)
    def reset_business():
        store.execute(
            f"DELETE FROM settings WHERE key IN ({','.join('?' * len(_SETTING_KEYS))})", _SETTING_KEYS
        )

    @router.get("/voices/catalog")
    def voices_catalog():
        return fetch_catalog(runtime.current(), store)

    @router.post("/voices/catalog/preview")
    def voices_catalog_preview(body: dict):
        """档案表单的音色候选：按被编辑档案（或表单内联值）的目标服务查询，而非当前激活后端。"""
        profile_id = body.get("profile_id")
        config = runtime.current()
        model = name = ""
        if profile_id is not None:
            row = store.one("SELECT * FROM model_profiles WHERE id=?", (int(profile_id),))
            if row is None:
                raise HTTPException(404, "档案不存在")
            profile_config = json.loads(row["config"])
            model = str(profile_config.get("model") or "")
            name = str(row["name"] or "")
            config = merge_profile(config, "tts", {**row, "config": profile_config})
        else:
            fields = {
                key: body[key]
                for key in ("backend", "base_url", "model")
                if isinstance(body.get(key), str) and body[key].strip()
            }
            model = str(fields.get("model") or "")
            name = str(body["name"]) if isinstance(body.get("name"), str) else ""
            if not fields:
                raise HTTPException(422, "请提供 profile_id 或表单连接字段")
            merged = SpeakerConfig.model_validate({**config.studio.tts.model_dump(), **fields})
            if isinstance(body.get("api_key"), str) and body["api_key"].strip():
                merged = merged.model_copy(update={"api_key": body["api_key"].strip()})
            config = config.model_copy(
                update={"studio": config.studio.model_copy(update={"tts": merged})}
            )
        catalog = fetch_catalog(config, store)
        kind = voice_kind(model, name)
        catalog["voice_kind"] = kind
        if kind == "clone":
            catalog["groups"] = []  # VoiceClone 型只允许音色库克隆音色，不给系统列表
            if not catalog["library"]:
                catalog["hint"] = "音色库中还没有当前厂商的克隆音色；请先在本页「音色库」上传样本克隆。"
        return catalog

    @router.post("/voices", status_code=201)
    def register_voice(body: VoiceBody):
        return _with_sample(
            save_voice(
                store,
                name=body.name,
                provider=body.provider,
                voice_id=body.voice_id,
                kind=body.kind,
                notes=body.notes,
            )
        )

    @router.delete("/voices/{voice_pk}", status_code=204)
    def remove_voice(voice_pk: int):
        voice = get_voice(store, voice_pk)
        if voice is None:
            raise HTTPException(404, "音色不存在")
        if get_setting(store, "override.studio.voice") == voice["voice_id"]:
            store.execute("DELETE FROM settings WHERE key='override.studio.voice'")
        delete_voice(store, voice_pk)

    @router.post("/voices/clone", status_code=201)
    async def clone_voice(
        name: str = Form(""),
        consent: bool = Form(False),
        audio_url: str = Form(""),
        ref_text: str = Form(""),
        file: Annotated[UploadFile | None, File()] = None,
    ):
        """按当前语音合成后端选择克隆方式：本地 qwentts / MiniMax 上传样本；阿里填公网 URL。"""
        name = name.strip()
        if not name:
            raise HTTPException(422, "请填写音色名称")
        if not consent:
            raise HTTPException(422, "请确认你拥有该声音的合法使用授权")
        current = runtime.current()
        tts = current.studio.tts
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "voice"
        directory = store.root / "voices"
        directory.mkdir(parents=True, exist_ok=True)

        async def clone_from_sample(provider: str, missing: str, clone, notes: str = "") -> dict:
            """上传样本类克隆（本地 qwentts、MiniMax）：限额落盘 → pending 记录 → 线程池克隆 → 转正。

            ``clone(sample, voice_id)`` 在线程池执行并返回试听音频（可为空）；失败时删除待定样本，
            记录标为 failed。
            """
            if file is None:
                raise HTTPException(422, missing)
            suffix = Path(file.filename or "").suffix.lower()
            if suffix not in _CLONE_SAMPLE_SUFFIXES:
                await file.close()
                raise HTTPException(422, "样本格式仅支持 wav/mp3/m4a")
            sample = directory / f"pending-{secrets.token_hex(4)}{suffix}"
            await save_upload(file, sample, MAX_VOICE_SAMPLE_BYTES)  # 超限 413，失败自行删除半成品
            voice_id = f"mindex-{slug}-{secrets.token_hex(3)}"
            voice = None
            try:
                voice = save_voice(
                    store, name=name, provider=provider, voice_id=voice_id,
                    kind="clone", sample_path=str(sample.relative_to(store.root)),
                    status="pending", notes=notes,
                )
                preview = await run_in_threadpool(clone, sample, voice_id)
            except Exception as exc:
                sample.unlink(missing_ok=True)
                if voice is not None:
                    save_voice(
                        store, name=name, provider=provider, voice_id=voice_id, kind="clone",
                        status="failed", notes=str(exc)[:400], voice_pk=voice["id"],
                    )
                log.warning("声音克隆失败（%s）", provider, exc_info=True)
                raise HTTPException(502, f"克隆失败：{exc}") from exc
            final_sample = directory / f"{voice['id']}{suffix}"

            def finish():
                sample.replace(final_sample)
                if preview:
                    (directory / f"{voice['id']}-preview.wav").write_bytes(preview)

            await run_in_threadpool(finish)
            return _with_sample(
                save_voice(
                    store, name=name, provider=provider, voice_id=voice_id,
                    kind="clone", sample_path=str(final_sample.relative_to(store.root)),
                    status="ready", notes=notes, voice_pk=voice["id"],
                )
            )

        if tts.backend == "openai":
            reference = ref_text.strip()

            def qwentts_clone(sample: Path, voice_id: str) -> bytes:
                qwentts_register(tts.base_url, sample, voice_id, reference, timeout=tts.timeout)
                return make_speaker(current).speak("你好，这是克隆音色的试听。", voice=voice_id)

            return await clone_from_sample(
                "self", "本地克隆需要上传声音样本（wav/mp3/m4a，3 秒以上清晰人声）",
                qwentts_clone, notes=reference[:400],
            )

        if tts.backend == "minimax":
            return await clone_from_sample(
                "minimax", "MiniMax 克隆需要上传声音样本（wav/mp3/m4a，10 秒到 5 分钟）",
                lambda sample, voice_id: minimax_clone(tts.base_url, tts.api_key, sample, voice_id, tts.model,
                                                           timeout=tts.timeout),
            )

        if tts.backend == "dashscope":
            if not audio_url.strip():
                raise HTTPException(422, "阿里复刻需要公网可访问的音频样本 URL（wav/mp3/m4a，10~20 秒）")
            try:
                voice_id = await run_in_threadpool(
                    aliyun_enroll, tts.base_url, tts.api_key, audio_url.strip(), f"mindex-{slug}", tts.model,
                    timeout=tts.timeout,
                )
            except Exception as exc:
                log.warning("阿里声音复刻失败", exc_info=True)
                # 失败记录的 voice_id 加随机后缀，重复失败不会冲突。
                save_voice(
                    store, name=name, provider="dashscope",
                    voice_id=f"mindex-{slug}-failed-{secrets.token_hex(3)}",
                    kind="clone", status="failed", notes=str(exc)[:400],
                )
                raise HTTPException(502, f"复刻失败：{exc}") from exc
            return _with_sample(
                save_voice(store, name=name, provider="dashscope", voice_id=voice_id, kind="clone")
            )

        raise HTTPException(
            422,
            "当前语音合成后端不支持克隆；请先激活 MiniMax（上传样本克隆）或阿里云（URL 复刻）档案",
        )

    return router
