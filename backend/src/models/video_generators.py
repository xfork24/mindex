"""AIGC 视频生成后端：MiniMax 海螺（video_generation）、阿里通义万相（DashScope 异步任务）、自建 ComfyUI。

三者都是「提交 → 轮询 → 下载」的异步协议，统一实现 ``adapters.VideoGenerator``。约定：

- 密钥只放在请求头（``Service.headers()``），诊断经 ``adapters.request(audit=...)`` 只记录脱敏后的请求体，
  内联参考图以 ``<data-url …>`` 占位，从不记录请求头；
- 外部服务的鉴权失败、内容审核拒绝、额度不足等映射为带中文说明的 ``ModelError``（审核拒绝为
  ``VideoContentRejected``）；
- ``health()`` 只做鉴权/可达性探测（查询接口或系统状态），绝不提交生成任务（不产生费用）。

厂商协议出处（核对日期 2026-10-25）：

- MiniMax H3 视频生成 V2（多模态 content：文生 / 首帧 / 尾帧 / 首尾帧 / 主体参考）：
  https://platform.minimax.cn/docs/api-reference/video-generation-v2-create （POST /v2/video_generation）
  https://platform.minimax.cn/docs/api-reference/video-generation-v2-query （GET /v2/query/video_generation/{task_id}，
  成功后 task.content.url 为成片地址）。取消接口路径未在文档中给出，取消时只停止轮询。
- MiniMax 视频生成：
  https://platform.minimax.io/docs/api-reference/video-generation-t2v （文生视频，POST /v1/video_generation）
  https://platform.minimax.io/docs/api-reference/video-generation-i2v （图生视频，first_frame_image）
  https://platform.minimax.io/docs/api-reference/video-generation-query （GET /v1/query/video_generation）
  https://platform.minimax.io/docs/api-reference/video-generation-download （GET /v1/files/retrieve）
  https://platform.minimax.io/docs/api-reference/errorcode （base_resp.status_code 错误码）
- 阿里云百炼 通义万相：
  https://help.aliyun.com/en/model-studio/text-to-video-api-reference （文生视频，parameters.size）
  https://help.aliyun.com/en/model-studio/image-to-video-api-reference （图生视频首帧，img_url + resolution）
  两者均为 POST /api/v1/services/aigc/video-generation/video-synthesis（请求头 X-DashScope-Async: enable），
  再 GET /api/v1/tasks/{task_id} 轮询（PENDING → RUNNING → SUCCEEDED/FAILED，video_url 24 小时有效）。
  本实现对应文档中「wan2.6 及更早」的旧协议（文生 parameters.size、图生 parameters.resolution）；wan2.7 的新协议
  改用 resolution + ratio，暂不支持。wan2.6 图生视频的 resolution 只接受 720P/1080P，480P 档位仅适用于更早的模型。
  文档推荐的工作空间专属域名（https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com）与旧域名协议一致，可直接填入 base_url。
- ComfyUI：POST /prompt、GET /history/{prompt_id}、GET /queue、GET /view、POST /upload/image、
  POST /interrupt、GET /system_stats（ComfyUI server.py 内置路由）。

未能从文档逐项确认、按最佳理解实现的部分（如有偏差以平台实际返回为准，均有测试桩覆盖解析逻辑）：
MiniMax 不接受 negative_prompt/seed，提交时忽略；健康探测用查询接口查一个不存在的任务，只把鉴权类错误码
（1004/2049）判为失败。DashScope 取消接口 POST /api/v1/tasks/{task_id}/cancel 只对排队中的任务生效，
失败时忽略；健康探测同样查询一个不存在的任务，只把 401/403/InvalidApiKey 判为失败。
"""

from __future__ import annotations

import base64
import copy
import mimetypes
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

import httpx

from mindex.models import adapters
from mindex.models.adapters import ModelError
from mindex.shared.comfyui import MAPPING_FIELDS, parse_target, validate_workflow, workflow_hash
from mindex.shared.config import Config
from mindex.shared.jsonio import save_bytes

VideoState = Literal["queued", "running", "succeeded", "failed"]
VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".gif", ".webp")
HEALTH_TASK_ID = "mindex-health-probe"  # 不存在的任务 ID：查询接口只用于验证鉴权与可达性


class VideoContentRejected(ModelError):
    """生成服务的内容审核拒绝了提示词、参考图或生成结果；需修改输入后重试。"""


@dataclass(frozen=True)
class VideoRequest:
    prompt: str
    negative_prompt: str | None = None
    image: Path | None = None  # 首帧参考图（本地文件）
    seconds: float = 5.0
    width: int = 1280
    height: int = 720
    seed: int | None = None
    # MiniMax H3（V2 多模态）专用；其余后端忽略（预检会拦下它们不支持的生成方式）。
    mode: str | None = None  # text / first_frame / last_frame / first_last / reference；None = 按输入推断
    last_image: Path | None = None  # 尾帧参考图
    references: tuple[tuple[str, Path], ...] = ()  # (image|video|audio, 本地文件)：多模态参考
    model: str | None = None  # 逐镜覆盖模型档位（如 MiniMax-H3-Highspeed）；None = 档案模型
    resolution: str | None = None  # 480P / 768P / 2K；None = 768P
    ratio: str | None = None  # adaptive / 21:9 / 16:9 / 4:3 / 1:1 / 3:4 / 9:16；None = 按画幅


@dataclass
class VideoStatus:
    state: VideoState
    url: str | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)


def allowed_seconds(durations, seconds: float) -> float:
    """配音时长 → 后端允许的生成时长：取不短于它的最小允许值；都更短时取最大值（不足部分冻结帧兜底）。"""
    options = sorted(float(value) for value in durations)
    for value in options:
        if value >= seconds - 1e-6:
            return value
    return options[-1]


def data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _redact_images(value):
    """审计用请求体：内联图片（data URL）只保留类型与长度。"""
    if isinstance(value, dict):
        return {key: _redact_images(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_images(item) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        return f"<data-url {value[5:].split(';', 1)[0]} {len(value)} 字符>"
    return value


def _error_body(exc: ModelError) -> tuple[int | None, dict]:
    """``adapters.request`` 包装的 HTTP 错误：取出状态码与 JSON 响应体（无法解析时为空）。"""
    cause = exc.__cause__
    if isinstance(cause, httpx.HTTPStatusError):
        try:
            body = cause.response.json()
        except ValueError:
            body = {}
        return cause.response.status_code, body if isinstance(body, dict) else {}
    return None, {}


class _Backend:
    label = "视频生成"

    def __init__(self, config: Config):
        self.profile = config.studio.video_generator

    def allowed_seconds(self, seconds: float) -> float:
        return allowed_seconds(self.profile.durations, seconds)

    def cancel(self, task_id: str) -> None:
        return None

    def _url(self, path: str) -> str:
        return self.profile.base_url.rstrip("/") + path

    def _download(self, url: str, target: Path, headers: dict | None = None) -> None:
        # 结果地址多为带签名的临时 URL：不附带鉴权头；诊断中的 URL 由 sanitized 去掉查询串。
        payload = adapters.request_bytes("GET", url, timeout=self.profile.timeout, headers=headers or {})
        if not payload:
            raise ModelError(f"{self.label}结果文件为空")
        save_bytes(target, payload)


# ---------------------------------------------------------------- MiniMax 海螺


MINIMAX_AUTH = {1004, 2049}
MINIMAX_QUOTA = {1008, 2053, 2056, 2067}
MINIMAX_RATE = {1002, 1039, 1041, 1045, 2045}
MINIMAX_MODEL = {2022, 2061, 2068, 2069}
MINIMAX_STATES = {"Preparing": "queued", "Queueing": "queued", "Processing": "running", "Success": "succeeded",
                  "Fail": "failed"}


def minimax_message(code: int, detail: str = "") -> str:
    suffix = f"（{detail}）" if detail else ""
    if code in MINIMAX_AUTH:
        return f"MiniMax 鉴权失败，请检查视频生成档案的 API Key{suffix}"
    if code in MINIMAX_QUOTA:
        return f"MiniMax 账户余额或额度不足，请充值或更换套餐后重试{suffix}"
    if code == 1026:
        return f"MiniMax 内容审核未通过：提示词或参考图含敏感内容，请修改后重试{suffix}"
    if code == 1027:
        return f"MiniMax 内容审核未通过：生成结果含敏感内容，请修改提示词后重试{suffix}"
    if code in MINIMAX_RATE:
        return f"MiniMax 限流或并发任务超限，请稍后重试{suffix}"
    if code in MINIMAX_MODEL:
        return f"MiniMax 视频模型不可用或当前套餐不支持，请检查模型名{suffix}"
    if code == 2013:
        return f"MiniMax 请求参数无效（时长、分辨率与模型组合可能不受支持）{suffix}"
    return f"MiniMax 视频生成接口返回错误 {code}{suffix}"


def _minimax_error(code: int, detail: str = "") -> ModelError:
    cls = VideoContentRejected if code in (1026, 1027) else ModelError
    return cls(minimax_message(code, detail))


class MiniMaxVideoGenerator(_Backend):
    """MiniMax 海螺视频：base_url 与语音合成预设一致（``https://api.minimax.cn/v1``）。

    文生视频与图生视频共用 /video_generation，参考图以 data URL 放在 ``first_frame_image``；接口没有
    negative_prompt、seed 与任意宽高参数，输出尺寸由模型/分辨率档位决定，渲染阶段再按画幅适配。
    """

    label = "MiniMax 视频生成"

    def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            payload = adapters.request(method, self._url(path), timeout=self.profile.timeout,
                                       headers=self.profile.headers(), **kwargs)
        except ModelError as exc:
            status, body = _error_body(exc)
            code = (body.get("base_resp") or {}).get("status_code")
            if code:
                raise _minimax_error(int(code), str((body.get("base_resp") or {}).get("status_msg") or "")) from exc
            if status in (401, 403):
                raise ModelError(minimax_message(1004)) from exc
            raise
        return payload

    @staticmethod
    def _check(payload: dict) -> None:
        base = payload.get("base_resp") or {}
        code = int(base.get("status_code") or 0)
        if code != 0:
            raise _minimax_error(code, str(base.get("status_msg") or ""))

    def submit(self, request: VideoRequest) -> str:
        body = {"model": self.profile.model, "prompt": request.prompt, "duration": round(request.seconds)}
        if request.image is not None:
            body["first_frame_image"] = data_url(request.image)
        payload = self._call("POST", "/video_generation", json=body, audit=_redact_images(body))
        self._check(payload)
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ModelError("MiniMax 视频生成未返回 task_id")
        return task_id

    def poll(self, task_id: str) -> VideoStatus:
        payload = self._call("GET", "/query/video_generation", params={"task_id": task_id})
        base = payload.get("base_resp") or {}
        code = int(base.get("status_code") or 0)
        status = str(payload.get("status") or "")
        state = MINIMAX_STATES.get(status)
        if code != 0 and status != "Fail":
            raise _minimax_error(code, str(base.get("status_msg") or ""))
        if state is None:
            raise ModelError(f"MiniMax 视频任务返回了未知状态：{status or '空'}")
        error = None
        if state == "failed":
            error = minimax_message(code, str(base.get("status_msg") or "")) if code else "MiniMax 视频生成失败"
        elif state == "succeeded" and not payload.get("file_id"):
            state, error = "failed", "MiniMax 视频任务已完成但没有返回 file_id"
        return VideoStatus(state=state, error=error, raw=payload)

    def fetch(self, status: VideoStatus, target: Path) -> None:
        payload = self._call("GET", "/files/retrieve", params={"file_id": status.raw.get("file_id")})
        self._check(payload)
        url = (payload.get("file") or {}).get("download_url")
        if not isinstance(url, str) or not url:
            raise ModelError("MiniMax 未返回视频下载地址")
        self._download(url, target)

    def health(self) -> dict:
        payload = self._call("GET", "/query/video_generation", params={"task_id": HEALTH_TASK_ID})
        code = int((payload.get("base_resp") or {}).get("status_code") or 0)
        if code in MINIMAX_AUTH:
            raise _minimax_error(code, str((payload.get("base_resp") or {}).get("status_msg") or ""))
        return {"backend": "minimax", "model": self.profile.model, "ready": True,
                "reply": "鉴权通过：查询接口可用（未发起生成）"}


# ---------------------------------------------------------------- MiniMax H3（视频生成 V2，多模态 content）


H3_STATES = {"queued": "queued", "running": "running", "succeeded": "succeeded", "failed": "failed",
             "cancelled": "failed"}
H3_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
H3_MODES = ("text", "first_frame", "last_frame", "first_last", "reference")
H3_MEDIA = {"image": ("image_url", "reference_image"), "video": ("video_url", "reference_video"),
            "audio": ("audio_url", "reference_audio")}


def nearest_ratio(width: int, height: int) -> str:
    """画幅 → H3 文生视频允许的最接近比例（文生视频不能用 adaptive）。"""
    target = width / height
    return min(H3_RATIOS, key=lambda r: abs(int(r.split(":")[0]) / int(r.split(":")[1]) - target))


def h3_mode(request: VideoRequest) -> str:
    """生成方式：显式给出时原样使用，否则按输入推断（参考素材 → 多模态参考；首尾帧图 → 图生视频）。"""
    if request.mode:
        return request.mode
    if request.references:
        return "reference"
    if request.image is not None and request.last_image is not None:
        return "first_last"
    if request.last_image is not None:
        return "last_frame"
    if request.image is not None:
        return "first_frame"
    return "text"


def h3_content(request: VideoRequest) -> tuple[list[dict], str]:
    """V2 请求的 content 数组与实际生成方式；输入与生成方式不符时报中文错误。"""
    mode = h3_mode(request)
    if mode not in H3_MODES:
        raise ModelError(f"MiniMax H3 不支持的生成方式：{mode}")
    content: list[dict] = [{"type": "text", "text": request.prompt}]

    def image(path: Path, role: str) -> dict:
        return {"type": "image_url", "image_url": {"url": data_url(path)}, "role": role}

    if mode == "first_frame":
        if request.image is None:
            raise ModelError("图生视频（首帧）需要首帧图片")
        content.append(image(request.image, "first_frame"))
    elif mode == "last_frame":
        if request.last_image is None:
            raise ModelError("图生视频（尾帧）需要尾帧图片")
        content.append(image(request.last_image, "last_frame"))
    elif mode == "first_last":
        if request.image is None or request.last_image is None:
            raise ModelError("首尾帧生视频需要首帧与尾帧两张图片")
        content += [image(request.image, "first_frame"), image(request.last_image, "last_frame")]
    elif mode == "reference":
        if not request.references:
            raise ModelError("主体参考生视频至少需要一个参考图片、视频或音频")
        if all(kind == "audio" for kind, _ in request.references):
            raise ModelError("主体参考生视频不能只有参考音频，请至少加入一张参考图或一段参考视频")
        for kind, path in request.references:
            field_type, role = H3_MEDIA[kind]
            content.append({"type": field_type, field_type: {"url": data_url(path)}, "role": role})
    return content, mode


class MiniMaxH3VideoGenerator(_Backend):
    """MiniMax H3 视频生成 V2：``POST /v2/video_generation`` 提交多模态 content，``GET /v2/query/video_generation/{id}``
    轮询，成功后从 ``task.content.url`` 下载。

    base_url 填平台地址（``https://api.minimax.cn``；误填 ``…/v1`` 时自动去掉）。生成方式、模型档位、分辨率、
    比例与时长由分镜的 aigc 来源逐镜给出；接口没有 negative_prompt/seed，提交时忽略。
    """

    label = "MiniMax H3 视频生成"

    def _url(self, path: str) -> str:
        base = self.profile.base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return base + path

    def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            payload = adapters.request(method, self._url(path), timeout=self.profile.timeout,
                                       headers=self.profile.headers(), **kwargs)
        except ModelError as exc:
            status, body = _error_body(exc)
            code = (body.get("base_resp") or {}).get("status_code")
            if code:
                raise _minimax_error(int(code), str((body.get("base_resp") or {}).get("status_msg") or "")) from exc
            if status in (401, 403):
                raise ModelError(minimax_message(1004)) from exc
            raise
        return payload

    def request_body(self, request: VideoRequest) -> dict:
        content, mode = h3_content(request)
        model = request.model or self.profile.model or "MiniMax-H3"
        floor = 5 if model.endswith("-Max") else 4
        duration = min(15, max(floor, round(request.seconds)))
        ratio = request.ratio
        if mode in ("first_frame", "last_frame", "first_last"):
            ratio = "adaptive"  # 图生视频的比例由输入图片决定
        elif mode == "text" and (not ratio or ratio == "adaptive"):
            ratio = nearest_ratio(request.width, request.height)
        elif not ratio:
            ratio = nearest_ratio(request.width, request.height)
        return {"model": model, "content": content, "resolution": request.resolution or "768P",
                "duration": duration, "ratio": ratio}

    def submit(self, request: VideoRequest) -> str:
        body = self.request_body(request)
        payload = self._call("POST", "/v2/video_generation", json=body, audit=_redact_images(body))
        MiniMaxVideoGenerator._check(payload)
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ModelError("MiniMax H3 视频生成未返回 task_id")
        return task_id

    def poll(self, task_id: str) -> VideoStatus:
        payload = self._call("GET", f"/v2/query/video_generation/{task_id}")
        MiniMaxVideoGenerator._check(payload)
        task = payload.get("task") or {}
        status = str(task.get("status") or "")
        state = H3_STATES.get(status)
        if state is None:
            raise ModelError(f"MiniMax H3 视频任务返回了未知状态：{status or '空'}")
        url = (task.get("content") or {}).get("url")
        error = None
        if status == "cancelled":
            error = "MiniMax H3 视频任务已被取消"
        elif state == "failed":
            error = str(task.get("error") or task.get("message") or "MiniMax H3 视频生成失败")
        elif state == "succeeded" and not url:
            state, error = "failed", "MiniMax H3 视频任务已完成但没有返回视频地址"
        return VideoStatus(state=state, url=url, error=error, raw=payload)

    def fetch(self, status: VideoStatus, target: Path) -> None:
        if not status.url:
            raise ModelError("MiniMax H3 未返回视频下载地址")
        self._download(status.url, target)

    def health(self) -> dict:
        try:
            payload = adapters.request("GET", self._url(f"/v2/query/video_generation/{HEALTH_TASK_ID}"),
                                       timeout=self.profile.timeout, headers=self.profile.headers(), attempts=1)
        except ModelError as exc:
            status, body = _error_body(exc)
            code = int((body.get("base_resp") or {}).get("status_code") or 0)
            if status in (401, 403) or code in MINIMAX_AUTH:
                raise ModelError(minimax_message(code or 1004)) from exc
            if status is None:
                raise  # 网络不可达
            # 查询不存在的任务返回 4xx：鉴权与可达性正常。
            return self._healthy()
        code = int((payload.get("base_resp") or {}).get("status_code") or 0)
        if code in MINIMAX_AUTH:
            raise _minimax_error(code, str((payload.get("base_resp") or {}).get("status_msg") or ""))
        return self._healthy()

    def _healthy(self) -> dict:
        return {"backend": "minimax_h3", "model": self.profile.model or "MiniMax-H3", "ready": True,
                "reply": "鉴权通过：查询接口可用（未发起生成）"}


# ---------------------------------------------------------------- 阿里通义万相（DashScope）


DASHSCOPE_STATES = {"PENDING": "queued", "RUNNING": "running", "SUCCEEDED": "succeeded", "FAILED": "failed",
                    "CANCELED": "failed", "UNKNOWN": "failed"}
# 文生视频 parameters.size 的可选分辨率（宽*高），按画幅挑最接近的一档。
DASHSCOPE_SIZES = ((832, 480), (480, 832), (624, 624), (1280, 720), (720, 1280), (960, 960), (1088, 832),
                   (832, 1088), (1920, 1080), (1080, 1920), (1440, 1440), (1632, 1248), (1248, 1632))


def dashscope_message(code: str, detail: str = "", status: int | None = None) -> str:
    suffix = f"（{detail}）" if detail else ""
    if code == "InvalidApiKey" or (status in (401, 403) and not code):
        return f"阿里云百炼鉴权失败，请检查视频生成档案的 API Key{suffix}"
    if code in ("DataInspectionFailed", "IPInfringementSuspect") or "DataInspection" in code:
        return f"通义万相内容审核未通过：提示词、参考图或生成结果含敏感/侵权风险内容，请修改后重试{suffix}"
    if code == "Arrearage":
        return f"阿里云百炼账户欠费，请充值后重试{suffix}"
    if "Quota" in code or "FreeTier" in code:
        return f"阿里云百炼额度不足或免费额度已用尽{suffix}"
    if code.startswith("Throttling"):
        return f"阿里云百炼限流，请稍后重试{suffix}"
    if code in ("InvalidParameter", "InvalidParameter.DataInspection"):
        return f"通义万相请求参数无效（时长、分辨率与模型组合可能不受支持）{suffix}"
    if code == "ModelNotFound" or code.startswith("Model."):
        return f"通义万相模型不可用，请检查模型名{suffix}"
    return f"通义万相视频生成接口返回错误 {code or status or '未知'}{suffix}"


def _dashscope_error(code: str, detail: str = "", status: int | None = None) -> ModelError:
    rejected = code in ("DataInspectionFailed", "IPInfringementSuspect") or "DataInspection" in code
    return (VideoContentRejected if rejected else ModelError)(dashscope_message(code, detail, status))


def nearest_size(width: int, height: int) -> str:
    ratio, pixels = width / height, width * height
    best = min(DASHSCOPE_SIZES, key=lambda s: (abs(s[0] / s[1] - ratio), abs(s[0] * s[1] - pixels)))
    return f"{best[0]}*{best[1]}"


def resolution_tier(width: int, height: int) -> str:
    pixels = width * height
    return "480P" if pixels <= 832 * 480 * 1.2 else "720P" if pixels <= 1280 * 720 * 1.2 else "1080P"


class DashScopeVideoGenerator(_Backend):
    """通义万相：base_url 与语音合成预设一致（``https://dashscope.aliyuncs.com``）。

    有参考图时走图生视频（img_url + resolution 档位，须配置 i2v 模型，如 wan2.6-i2v-flash）；
    否则走文生视频（size 宽*高，须配置 t2v 模型，如 wan2.6-t2v）。
    """

    label = "通义万相视频生成"

    def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            return adapters.request(method, self._url(path), timeout=self.profile.timeout,
                                    headers={**self.profile.headers(), **kwargs.pop("headers", {})}, **kwargs)
        except ModelError as exc:
            status, body = _error_body(exc)
            code = str(body.get("code") or "")
            if code or status in (401, 403):
                raise _dashscope_error(code, str(body.get("message") or ""), status) from exc
            raise

    def submit(self, request: VideoRequest) -> str:
        source = {"prompt": request.prompt}
        if request.negative_prompt:
            source["negative_prompt"] = request.negative_prompt
        parameters: dict = {"duration": round(request.seconds)}
        if request.image is not None:
            source["img_url"] = data_url(request.image)
            parameters["resolution"] = resolution_tier(request.width, request.height)
        else:
            parameters["size"] = nearest_size(request.width, request.height)
        if request.seed is not None:
            parameters["seed"] = request.seed
        body = {"model": self.profile.model, "input": source, "parameters": parameters}
        payload = self._call("POST", "/api/v1/services/aigc/video-generation/video-synthesis",
                             headers={"X-DashScope-Async": "enable"}, json=body, audit=_redact_images(body))
        output = payload.get("output") or {}
        if payload.get("code"):
            raise _dashscope_error(str(payload["code"]), str(payload.get("message") or ""))
        task_id = output.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ModelError("通义万相未返回 task_id")
        return task_id

    def poll(self, task_id: str) -> VideoStatus:
        payload = self._call("GET", f"/api/v1/tasks/{task_id}")
        output = payload.get("output") or {}
        status = str(output.get("task_status") or "")
        state = DASHSCOPE_STATES.get(status)
        if state is None:
            raise ModelError(f"通义万相任务返回了未知状态：{status or '空'}")
        error = None
        if state == "failed":
            code, detail = str(output.get("code") or ""), str(output.get("message") or "")
            if status == "CANCELED":
                error = "通义万相任务已被取消"
            elif status == "UNKNOWN":
                error = "通义万相任务不存在或已过期（任务 ID 只在 24 小时内可查询）"
            else:
                error = dashscope_message(code, detail) if code else f"通义万相视频生成失败{f'：{detail}' if detail else ''}"
        url = output.get("video_url") if state == "succeeded" else None
        if state == "succeeded" and not url:
            state, error = "failed", "通义万相任务已完成但没有返回 video_url"
        return VideoStatus(state=state, url=url, error=error, raw=payload)

    def fetch(self, status: VideoStatus, target: Path) -> None:
        if not status.url:
            raise ModelError("通义万相没有可下载的视频地址")
        self._download(status.url, target)

    def cancel(self, task_id: str) -> None:
        try:
            self._call("POST", f"/api/v1/tasks/{task_id}/cancel", attempts=1)
        except ModelError:
            pass  # 只有排队中的任务可取消；运行中的任务会继续完成，结果不被选用

    def health(self) -> dict:
        try:
            adapters.request("GET", self._url(f"/api/v1/tasks/{HEALTH_TASK_ID}"), timeout=self.profile.timeout,
                             headers=self.profile.headers(), attempts=1)
        except ModelError as exc:
            status, body = _error_body(exc)
            code = str(body.get("code") or "")
            if status is None:
                raise  # 连接失败、超时等：如实报告
            if status in (401, 403) or code == "InvalidApiKey":
                raise _dashscope_error(code or "InvalidApiKey", str(body.get("message") or ""), status) from exc
            # 其余 4xx（任务不存在等）说明请求已通过鉴权
        return {"backend": "dashscope", "model": self.profile.model, "ready": True,
                "reply": "鉴权通过：任务查询接口可用（未发起生成）"}


# ---------------------------------------------------------------- ComfyUI


class ComfyUIVideoGenerator(_Backend):
    """自建 ComfyUI：把请求字段按映射写入 API 格式工作流后提交到队列，完成后从历史记录取输出文件。"""

    label = "ComfyUI 视频生成"
    noun = "视频"
    role = "视频生成"
    suffixes = VIDEO_SUFFIXES
    output_keys = ("videos", "gifs", "images")
    fields = MAPPING_FIELDS

    def __init__(self, config: Config, profile=None):
        super().__init__(config)
        if profile is not None:
            self.profile = profile
        self._missing: dict[str, int] = {}

    def _call(self, method: str, path: str, **kwargs) -> dict:
        return adapters.request(method, self._url(path), timeout=self.profile.timeout,
                                headers=self.profile.headers(), **kwargs)

    def _workflow(self) -> tuple[dict, dict]:
        workflow, mapping = self.profile.comfyui_workflow, self.profile.comfyui_mapping
        if workflow is None or not mapping:
            raise ModelError(f"ComfyUI {self.role}未导入工作流或节点映射，请在设置页的{self.role}档案中补全")
        try:
            validate_workflow(workflow, mapping, self.fields)
        except ValueError as exc:
            raise ModelError(f"ComfyUI 工作流无效：{exc}") from exc
        return copy.deepcopy(workflow), mapping

    def _upload(self, image: Path) -> str:
        mime = mimetypes.guess_type(image.name)[0] or "image/png"
        payload = self._call(
            "POST", "/upload/image",
            files={"image": (f"mindex-{image.name}", image.read_bytes(), mime)},
            data={"overwrite": "true", "type": "input"},
        )
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise ModelError("ComfyUI 上传参考图后未返回文件名")
        subfolder = payload.get("subfolder") or ""
        return f"{subfolder}/{name}" if subfolder else name

    def submit(self, request: VideoRequest) -> str:
        _, mapping = self._workflow()
        if request.image is not None and "image" not in mapping:
            raise ModelError("分镜带有参考图，但 ComfyUI 节点映射没有 image 字段；请映射加载图片节点的 image 输入")
        values = {
            "prompt": request.prompt,
            "negative": request.negative_prompt,
            "seed": request.seed,
            "frames": max(1, round(request.seconds * self.profile.fps)),
            "width": request.width,
            "height": request.height,
        }
        if request.image is not None:
            values["image"] = self._upload(request.image)
        return self.submit_values(values)

    def submit_values(self, values: dict) -> str:
        """按节点映射把 ``values`` 写入工作流并提交；值为 None 的字段保留工作流原值。"""
        workflow, mapping = self._workflow()
        applied = {}
        for name, target in mapping.items():
            value = values.get(name)
            if value is None:  # 未提供（例如无反向提示词、随机种子）时保留工作流原值
                continue
            node_id, key = parse_target(target)
            workflow[node_id]["inputs"][key] = value
            applied[name] = value
        client_id = uuid.uuid4().hex
        payload = self._call(
            "POST", "/prompt", json={"prompt": workflow, "client_id": client_id},
            audit={"client_id": client_id, "workflow": workflow_hash(self.profile.comfyui_workflow),
                   "inputs": applied},
        )
        if payload.get("node_errors"):
            raise ModelError(f"ComfyUI 拒绝工作流：{str(payload['node_errors'])[:300]}")
        prompt_id = payload.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ModelError("ComfyUI 未返回 prompt_id")
        return prompt_id

    def _output(self, entry: dict) -> dict | None:
        for node in (entry.get("outputs") or {}).values():
            if not isinstance(node, dict):
                continue
            for key in self.output_keys:
                for item in node.get(key) or []:
                    if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(self.suffixes):
                        return item
        return None

    def poll(self, task_id: str) -> VideoStatus:
        history = self._call("GET", f"/history/{task_id}")
        entry = history.get(task_id) if isinstance(history, dict) else None
        if not entry:
            queue = self._call("GET", "/queue")
            if any(len(item) > 1 and item[1] == task_id for item in queue.get("queue_pending") or []):
                self._missing.pop(task_id, None)
                return VideoStatus(state="queued", raw=queue)
            if any(len(item) > 1 and item[1] == task_id for item in queue.get("queue_running") or []):
                self._missing.pop(task_id, None)
                return VideoStatus(state="running", raw=queue)
            self._missing[task_id] = self._missing.get(task_id, 0) + 1
            if self._missing[task_id] >= 3:
                return VideoStatus(state="failed", error="ComfyUI 中找不到该任务（服务可能已重启或任务被删除）",
                                   raw=queue)
            return VideoStatus(state="running", raw=queue)
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            detail = next(
                (str(data.get("exception_message") or "").strip()
                 for kind, data in status.get("messages") or []
                 if kind == "execution_error" and isinstance(data, dict)),
                "",
            )
            return VideoStatus(state="failed", error=f"ComfyUI 执行失败{f'：{detail[:300]}' if detail else ''}",
                               raw={"status": status})
        output = self._output(entry)
        if output is None:
            if status.get("completed") is False:
                return VideoStatus(state="running", raw={"status": status})
            return VideoStatus(state="failed", error=f"ComfyUI 工作流已完成但没有输出{self.noun}文件，请检查保存{self.noun}节点",
                               raw={"status": status, "outputs": entry.get("outputs")})
        query = urlencode({"filename": output["filename"], "subfolder": output.get("subfolder") or "",
                           "type": output.get("type") or "output"})
        return VideoStatus(state="succeeded", url=self._url(f"/view?{query}"), raw={"output": output})

    def fetch(self, status: VideoStatus, target: Path) -> None:
        if not status.url:
            raise ModelError("ComfyUI 没有可下载的输出文件")
        self._download(status.url, target, headers=self.profile.headers())

    def run(self, values: dict, target: Path, *, sleep=None, clock=None) -> None:
        """同步执行（图片/音乐生成）：提交 → 按 poll_interval 轮询到完成（不超过 max_wait）→ 下载到 ``target``。"""
        import time

        sleep, clock = sleep or time.sleep, clock or time.monotonic
        task_id = self.submit_values(values)
        deadline = clock() + self.profile.max_wait
        while True:
            status = self.poll(task_id)
            if status.state == "succeeded":
                self.fetch(status, target)
                return
            if status.state == "failed":
                raise ModelError(status.error or f"ComfyUI {self.role}失败")
            if clock() >= deadline:
                self.cancel(task_id)
                raise ModelError(f"ComfyUI {self.role}超时：{self.profile.max_wait:g} 秒内未完成，已尝试撤销任务")
            sleep(self.profile.poll_interval)

    def cancel(self, task_id: str) -> None:
        try:
            queue = self._call("GET", "/queue", attempts=1)
            if any(len(item) > 1 and item[1] == task_id for item in queue.get("queue_pending") or []):
                self._call("POST", "/queue", json={"delete": [task_id]}, attempts=1)
            elif any(len(item) > 1 and item[1] == task_id for item in queue.get("queue_running") or []):
                self._call("POST", "/interrupt", json={"prompt_id": task_id}, attempts=1)
        except ModelError:
            pass  # 尽力取消：服务不可达时任务结果不会被选用

    def health(self) -> dict:
        stats = self._call("GET", "/system_stats", attempts=1)
        system = stats.get("system") or {}
        configured = self.profile.comfyui_workflow is not None and bool(self.profile.comfyui_mapping)
        if configured:
            try:
                validate_workflow(self.profile.comfyui_workflow, self.profile.comfyui_mapping, self.fields)
            except ValueError as exc:
                raise ModelError(f"ComfyUI 工作流无效：{exc}") from exc
        return {
            "backend": "comfyui",
            "model": self.profile.model,
            "ready": configured,
            "version": system.get("comfyui_version"),
            "devices": len(stats.get("devices") or []),
            "reply": "服务可达，工作流与节点映射有效（未发起生成）" if configured
            else "服务可达；尚未导入工作流或节点映射",
        }
