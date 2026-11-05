"""第三方工具产物：下载（主机白名单、大小上限、流式、手动跟随重定向）与类型校验（ffprobe / Pillow）。

适配器返回 ``Artifact``：远端地址（``url``）或内联内容（``data``，MCP 的图片 / 内嵌资源）。校验通过后统一为
``artifact.mp4``（视频，能直接封装就不重编码）或 ``artifact.png``（图片）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from mindex.shared.media import MediaError, atomic_media, probe
from mindex.shared.work_budget import GrowingReservation, current
from mindex.studio import studio

MAX_REDIRECTS = 3


@dataclass
class Artifact:
    url: str | None = None
    data: bytes | None = None
    mime: str | None = None


def check_host(url: str, hosts: set[str]) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise studio.StudioError(f"产物地址不是 http(s)：{url[:200]}")
    if parts.netloc.lower() not in hosts and parts.hostname.lower() not in hosts:
        raise studio.StudioError(f"产物主机 {parts.netloc} 不在该工具源的下载主机白名单中")


def download(artifact: Artifact, target: Path, *, hosts: set[str], max_bytes: int, timeout: float,
             headers: dict | None = None) -> Path:
    """把产物写到 ``target``；超过大小上限、主机不在白名单或下载失败时报中文错误（不留半截文件）。"""
    import httpx

    target.parent.mkdir(parents=True, exist_ok=True)
    if artifact.data is not None:
        if len(artifact.data) > max_bytes:
            raise studio.StudioError(f"产物超过大小上限 {max_bytes // (1024 * 1024)} MB")
        studio.save_bytes(target, artifact.data)
        return target
    url = artifact.url or ""
    partial = target.with_name("." + target.name + ".partial")
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                check_host(url, hosts)
                with client.stream("GET", url, headers=headers or {}) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    if response.status_code >= 400:
                        raise studio.StudioError(f"下载产物失败：HTTP {response.status_code}")
                    length = response.headers.get("content-length")
                    if length and length.isdigit() and int(length) > max_bytes:
                        raise studio.StudioError(f"产物超过大小上限 {max_bytes // (1024 * 1024)} MB")
                    size = 0
                    with GrowingReservation(partial) as reservation, partial.open("wb") as handle:
                        for chunk in response.iter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > max_bytes:
                                raise studio.StudioError(f"产物超过大小上限 {max_bytes // (1024 * 1024)} MB")
                            reservation.grow(len(chunk))
                            handle.write(chunk)
                        handle.flush()
                        budget = current()
                        if budget is not None:
                            budget.retain(target)
                        reservation.commit(target)
                    return target
            raise studio.StudioError("产物地址重定向次数过多")
    except httpx.HTTPError as exc:
        raise studio.StudioError(f"下载产物失败：{exc}") from exc
    finally:
        partial.unlink(missing_ok=True)


def normalize(download_path: Path, directory: Path, output: str, max_pixels: int) -> tuple[Path, dict]:
    """校验并规整产物：视频 → ``artifact.mp4``（返回时长、尺寸、帧率），图片 → ``artifact.png``（返回尺寸）。"""
    if output == "image":
        from PIL import Image

        from mindex.shared.images import image_png

        try:
            payload = image_png(download_path, max_pixels)
        except (ValueError, OSError) as exc:
            raise studio.StudioError(f"工具产物不是可用的图片：{exc}") from exc
        target = directory / "artifact.png"
        studio.save_bytes(target, payload)
        with Image.open(target) as image:
            return target, {"width": image.width, "height": image.height}
    target = directory / "artifact.mp4"
    maps = ["-map", "0:v:0", "-map", "0:a:0?"]
    try:
        try:
            atomic_media(["-i", str(download_path), *maps, "-c", "copy", "-movflags", "+faststart"], target)
        except MediaError:
            atomic_media(["-i", str(download_path), *maps, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset",
                          "veryfast", "-crf", "18", "-c:a", "aac", "-movflags", "+faststart"], target)
        info = probe(target)
    except MediaError as exc:
        target.unlink(missing_ok=True)
        raise studio.StudioError(f"工具产物不是可解码的视频：{exc}") from exc
    return target, {key: info.get(key) for key in ("duration", "width", "height", "fps", "audio")}
