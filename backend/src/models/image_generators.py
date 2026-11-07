"""AI 生成图片后端：MiniMax image-01（文生图 / 主体参考图生图），实现 ``adapters.ImageGenerator``。

协议出处（核对日期 2026-10-25）：

- 文生图：https://platform.minimax.cn/docs/api-reference/image-generation-t2i （POST /v1/image_generation）
- 图生图：https://platform.minimax.cn/docs/api-reference/image-generation-i2i （同一接口，subject_reference
  人物主体参考，image_file 为公网 URL 或 data URL）

请求只取一张图（n=1）并以 base64 返回，结果立即落盘（URL 只有 24 小时有效）；宽高比按方案画幅映射到
``aspect_ratio``（16:9 / 9:16 / 1:1）。``health()`` 只发送缺少 prompt 的请求：鉴权失败与参数错误的错误码不同，
可在不生成图片（不计费）的前提下验证密钥与地址。
"""

from __future__ import annotations

import base64
import tempfile
from dataclasses import dataclass
from pathlib import Path

from models import adapters
from models.adapters import ModelError
from models.video_generators import MINIMAX_AUTH, ComfyUIVideoGenerator, _error_body, _redact_images, data_url
from shared.comfyui import IMAGE_MAPPING_FIELDS
from shared.config import Config

IMAGE_RATIOS = ("1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9")


class ImageContentRejected(ModelError):
    """图片生成服务的内容审核拒绝了提示词、参考图或生成结果。"""


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    aspect_ratio: str = "16:9"
    reference: Path | None = None  # 主体参考图（图生图）
    seed: int | None = None
    prompt_optimizer: bool = False
    width: int = 1280  # ComfyUI 工作流的宽高映射（MiniMax 用 aspect_ratio）
    height: int = 720
    negative_prompt: str | None = None


def nearest_aspect(width: int, height: int) -> str:
    target = width / height
    return min(IMAGE_RATIOS, key=lambda r: abs(int(r.split(":")[0]) / int(r.split(":")[1]) - target))


def image_message(code: int, detail: str = "") -> str:
    suffix = f"（{detail}）" if detail else ""
    if code in MINIMAX_AUTH:
        return f"MiniMax 鉴权失败，请检查图片生成档案的 API Key{suffix}"
    if code in (1008, 2053, 2056, 2067):
        return f"MiniMax 账户余额或额度不足，请充值或更换套餐后重试{suffix}"
    if code in (1026, 1027):
        return f"MiniMax 内容审核未通过：提示词或参考图含敏感内容，请修改后重试{suffix}"
    if code in (1002, 1039, 1041, 1045, 2045):
        return f"MiniMax 限流，请稍后重试{suffix}"
    if code == 2013:
        return f"MiniMax 图片生成请求参数无效{suffix}"
    return f"MiniMax 图片生成接口返回错误 {code}{suffix}"


def _error(code: int, detail: str = "") -> ModelError:
    return (ImageContentRejected if code in (1026, 1027) else ModelError)(image_message(code, detail))


class MiniMaxImageGenerator:
    """MiniMax image-01：base_url 与语音合成预设一致（``https://api.minimax.cn/v1``）。"""

    def __init__(self, config: Config):
        self.profile = config.studio.image_generator

    def _url(self) -> str:
        return self.profile.base_url.rstrip("/") + "/image_generation"

    def _call(self, body: dict, *, attempts: int = 3) -> dict:
        try:
            payload = adapters.request(
                "POST",
                self._url(),
                timeout=self.profile.timeout,
                headers=self.profile.headers(),
                json=body,
                audit=_redact_images(body),
                attempts=attempts,
            )
        except ModelError as exc:
            status, error = _error_body(exc)
            code = (error.get("base_resp") or {}).get("status_code")
            if code:
                raise _error(int(code), str((error.get("base_resp") or {}).get("status_msg") or "")) from exc
            if status in (401, 403):
                raise ModelError(image_message(1004)) from exc
            raise
        return payload

    def request_body(self, request: ImageRequest) -> dict:
        body = {
            "model": self.profile.model,
            "prompt": request.prompt,
            "aspect_ratio": request.aspect_ratio,
            "response_format": "base64",
            "n": 1,
            "prompt_optimizer": request.prompt_optimizer,
        }
        if request.seed is not None:
            body["seed"] = request.seed
        if request.reference is not None:
            body["subject_reference"] = [{"type": "character", "image_file": data_url(request.reference)}]
        return body

    def generate(self, request: ImageRequest) -> bytes:
        payload = self._call(self.request_body(request))
        base = payload.get("base_resp") or {}
        code = int(base.get("status_code") or 0)
        if code:
            raise _error(code, str(base.get("status_msg") or ""))
        images = (payload.get("data") or {}).get("image_base64") or []
        if not images:
            failed = (payload.get("metadata") or {}).get("failed_count")
            if failed and str(failed) != "0":
                raise ImageContentRejected(image_message(1027))
            raise ModelError("MiniMax 图片生成没有返回图片")
        try:
            return base64.b64decode(images[0])
        except ValueError as exc:
            raise ModelError("MiniMax 图片生成返回的图片无法解码") from exc

    def health(self) -> dict:
        return minimax_health(
            self._url(),
            self.profile,
            {"model": self.profile.model},
            "鉴权通过：图片生成接口可用（未生成图片）",
            "图片生成",
        )


def minimax_health(url: str, profile, body: dict, reply: str, noun: str) -> dict:
    """不计费的鉴权探测：发送缺少必填参数的请求，只有鉴权类错误（401/403、1004/2049）与网络不可达判为失败。"""
    try:
        payload = adapters.request(
            "POST", url, timeout=profile.timeout, headers=profile.headers(), json=body, attempts=1
        )
    except ModelError as exc:
        status, error = _error_body(exc)
        code = int((error.get("base_resp") or {}).get("status_code") or 0)
        if status in (401, 403) or code in MINIMAX_AUTH:
            raise ModelError(f"MiniMax 鉴权失败，请检查{noun}档案的 API Key") from exc
        if status is None:
            raise
        payload = error
    code = int((payload.get("base_resp") or {}).get("status_code") or 0)
    if code in MINIMAX_AUTH:
        raise ModelError(f"MiniMax 鉴权失败，请检查{noun}档案的 API Key")
    return {"backend": "minimax", "model": profile.model, "ready": True, "reply": reply}


class ComfyUIImageGenerator:
    """本地 ComfyUI 图片生成：把提示词、种子、宽高与主体参考图按节点映射写入「导出（API）」工作流，
    完成后从历史记录取第一张输出图片。"""

    def __init__(self, config: Config):

        class Runner(ComfyUIVideoGenerator):
            label, noun, role = "ComfyUI 图片生成", "图片", "图片生成"
            suffixes = (".png", ".jpg", ".jpeg", ".webp")
            output_keys = ("images",)
            fields = IMAGE_MAPPING_FIELDS

        self.profile = config.studio.image_generator
        self.runner = Runner(config, self.profile)

    def generate(self, request: ImageRequest) -> bytes:

        mapping = self.profile.comfyui_mapping or {}
        if request.reference is not None and "image" not in mapping:
            raise ModelError(
                "分镜带有主体参考图，但 ComfyUI 节点映射没有 image 字段；请映射加载图片节点的 image 输入"
            )
        values = {
            "prompt": request.prompt,
            "negative": request.negative_prompt,
            "seed": request.seed,
            "width": request.width,
            "height": request.height,
        }
        if request.reference is not None:
            values["image"] = self.runner._upload(request.reference)
        with tempfile.TemporaryDirectory(prefix="comfyui-image-") as directory:
            target = Path(directory) / "image.bin"
            self.runner.run(values, target)
            return target.read_bytes()

    def health(self) -> dict:
        return self.runner.health()
