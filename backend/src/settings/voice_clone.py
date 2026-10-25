"""厂商声音克隆客户端。

- MiniMax：multipart 上传样本（purpose=voice_clone）→ voice_clone 生成自定义音色，
  返回预览音频；之后 t2a_v2 直接用该 voice_id 合成。
- 阿里百炼：DashScope 声音复刻（voice-enrollment），要求样本是公网可访问 URL；
  复刻音色与合成模型绑定（target_model）。
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

from mindex.models import adapters
from mindex.models.adapters import ModelError
from mindex.shared.media import extract_audio

SAMPLE_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}


def minimax_clone(
    base_url: str,
    api_key: str,
    sample: Path,
    voice_id: str,
    model: str,
    preview_text: str = "你好，这是克隆音色的连接测试。",
    *,
    timeout: float,
) -> bytes:
    """上传样本并克隆；成功返回预览音频字节（hex 编码解码而来）。timeout 为语音合成档案的请求超时。"""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    mime = SAMPLE_MIME.get(sample.suffix.lower(), "application/octet-stream")
    with sample.open("rb") as handle:
        upload = adapters.request(
            "POST",
            base_url.rstrip("/") + "/files/upload",
            timeout=timeout,
            headers=headers,
            data={"purpose": "voice_clone"},
            files={"file": (sample.name, handle, mime)},
        )
    file_id = (upload.get("file") or {}).get("file_id")
    if not file_id:
        raise ModelError(f"MiniMax 样本上传未返回 file_id：{json.dumps(upload, ensure_ascii=False)[:200]}")
    payload = adapters.request(
        "POST",
        base_url.rstrip("/") + "/voice_clone",
        timeout=timeout,
        headers=headers,
        json={"file_id": file_id, "voice_id": voice_id, "model": model, "text": preview_text},
    )
    status = (payload.get("base_resp") or {}).get("status_code", 0)
    if status != 0:
        raise ModelError(f"MiniMax 声音克隆失败：{payload.get('base_resp')}")
    audio = (payload.get("data") or {}).get("audio")
    if isinstance(audio, str) and audio:
        try:
            return bytes.fromhex(audio)
        except ValueError as exc:
            raise ModelError("MiniMax 克隆预览音频 hex 解码失败") from exc
    return b""


def qwentts_register(base_url: str, sample: Path, name: str, ref_text: str = "", *, timeout: float) -> None:
    """向本地 qwentts.cpp tts-server 注册克隆音色（POST /audio/voices）。

    服务端注册表存于进程内存，重启即丢；调用方需持久化样本以便重注册（自愈）。
    非 wav 样本先转 16k 单声道 wav；ref_text 提供时启用 ICL 克隆（效果更好）。
    """
    if sample.suffix.lower() == ".wav":
        payload = sample.read_bytes()
    else:
        with tempfile.TemporaryDirectory(prefix="qwentts-") as directory:
            converted = Path(directory) / "sample.wav"
            extract_audio(sample, converted)
            payload = converted.read_bytes()
    adapters.request(
        "POST",
        base_url.rstrip("/") + "/audio/voices",
        timeout=timeout,
        json={"name": name, "ref_text": ref_text, "wav_b64": base64.b64encode(payload).decode()},
    )


def aliyun_enroll(base_url: str, api_key: str, audio_url: str, prefix: str, target_model: str, *,
                  timeout: float) -> str:
    """按公网 URL 提交复刻，返回生成的音色 voice_id（与 target_model 绑定）。"""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = adapters.request(
        "POST",
        base_url.rstrip("/") + "/api/v1/services/audio/tts/customization",
        timeout=timeout,
        headers=headers,
        json={
            "model": "voice-enrollment",
            "input": {
                "action": "create_voice",
                "target_model": target_model,
                "prefix": prefix,
                "url": audio_url,
            },
        },
    )
    output = payload.get("output") if isinstance(payload, dict) else None
    voice_id = (output or {}).get("voice_id") or (payload.get("voice_id") if isinstance(payload, dict) else None)
    if not voice_id:
        raise ModelError(
            "阿里声音复刻未返回音色 ID，请在百炼控制台确认结果后改用「录入已有音色」："
            + json.dumps(payload, ensure_ascii=False)[:200]
        )
    return str(voice_id)
