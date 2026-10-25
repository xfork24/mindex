"""仓库内测试用 MCP 服务器（spec m3-post-production 任务 24.2）：``python mcp_tool_server.py``（stdio）或
``python mcp_tool_server.py http 端口``（streamable-http，路径 /mcp）。

工具：make_image（PNG 图片内容）、make_video（内嵌 MP4 二进制资源）、count_uploads（返回收到的上传数与一张图片）、
broken（返回错误）。
"""

from __future__ import annotations

import base64
import io
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import BlobResourceContents, EmbeddedResource, TextContent

server = FastMCP("mindex-test-tools")


def png(color: str) -> bytes:
    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.new("RGB", (160, 120), color).save(buffer, "PNG")
    return buffer.getvalue()


@server.tool()
def make_image(color: str = "red") -> Image:
    """生成一张纯色图片。"""
    return Image(data=png(color), format="png")


@server.tool()
def make_video(seconds: float = 2.0) -> EmbeddedResource:
    """生成一段测试视频（160×120）。"""
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "clip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc=size=160x120:rate=25:duration={seconds}", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                        "-preset", "ultrafast", str(target)], check=True)
        blob = base64.b64encode(target.read_bytes()).decode()
    return EmbeddedResource(type="resource", resource=BlobResourceContents(
        uri="file:///clip.mp4", mimeType="video/mp4", blob=blob))


@server.tool()
def count_uploads(uploads: list[dict] | None = None) -> list:
    """返回收到的上传素材数，并附一张图片作为产物。"""
    return [TextContent(type="text", text=f"uploads={len(uploads or [])}"), Image(data=png("blue"), format="png")]


@server.tool()
def broken() -> str:
    """总是失败。"""
    raise ValueError("tool exploded")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "http":
        server.settings.port = int(sys.argv[2])
        server.run("streamable-http")
    else:
        server.run()
