from __future__ import annotations

import sys
from pathlib import Path

# 查询根目录修正:包内组件以短名导入(shared.media / cli 等)。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

import io
import math
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from shared.config import Config
from shared.media import command
from shared.types import Annotation, ShotDraft

pytest_plugins = ["ci_selection"]


@pytest.fixture(autouse=True)
def close_local_vector_clients(monkeypatch, tmp_path):
    """用例结束释放其临时向量库，避免长套件耗尽 SQLite 句柄与后台线程。

    只管理函数级 tmp_path 下的客户端；模块/会话级共享库保留原有生命周期。
    """
    import chromadb

    original = chromadb.PersistentClient
    clients = []

    def persistent_client(*args, **kwargs):
        path = kwargs.get("path", args[0] if args else "./chroma")
        client = original(*args, **kwargs)
        if Path(path).resolve().is_relative_to(tmp_path.resolve()):
            clients.append(client)
        return client

    monkeypatch.setattr(chromadb, "PersistentClient", persistent_client)
    yield
    for client in reversed(clients):
        client.close()


@pytest.fixture(scope="session")
def backup_seed(tmp_path_factory, video_file):
    """只构建一次备份种子；每个备份测试复制到独立目录后再修改。"""
    from test_backup import seed

    return seed(tmp_path_factory.mktemp("backup-seed") / "origin", video_file)


@pytest.fixture(autouse=True)
def offline_model_network(monkeypatch):
    """默认测试只访问本机隔离服务，禁止落到 Config 示例地址上的真实模型。"""
    import httpx

    original = httpx.HTTPTransport.handle_request
    original_async = httpx.AsyncHTTPTransport.handle_async_request

    def check(request):
        if request.url.host not in {"127.0.0.1", "localhost", "::1"}:
            raise httpx.ConnectError("离线测试禁止访问非本机服务；请使用隔离替身", request=request)

    def handle(self, request):
        check(request)
        return original(self, request)

    async def handle_async(self, request):
        check(request)
        return await original_async(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async)


@pytest.fixture(scope="session")
def video_file(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")
    path = root / "测试素材.mp4"
    command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x120:r=25:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=25:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=160x120:r=25:d=8",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    return path


@pytest.fixture
def config(tmp_path):
    config = Config(data_dir=tmp_path / "data")
    config.embedding.dimensions = 8
    return config


class FixtureEmbedder:
    """Tests only: color-based vectors, never exposed as a usable retrieval model."""

    def __init__(self, config: Config):
        self.config = config
        self.calls = 0

    def identity(self):
        return {
            "model": self.config.embedding.model,
            "revision": "fixture-revision",
            "quantization": self.config.embedding.quantization,
            "dimensions": 8,
            "normalized": True,
            "fps": self.config.video.fps,
            "max_frames": self.config.video.max_frames,
            "max_pixels": self.config.video.max_pixels,
            "implementation": "test-only",
        }

    def text(self, texts, role="document"):
        self.calls += len(texts)
        results = []
        for text in texts:
            vector = np.zeros(8, dtype=np.float32)
            vector[0 if "红" in text else 1 if "蓝" in text else 2] = 1
            results.append(vector.tolist())
        return results

    def video(self, path: Path):
        self.calls += 1
        raw = command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=1:1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ]
        )
        color = np.frombuffer(raw, np.uint8)
        vector = np.zeros(8)
        # RGB channel order -> textual red, blue, green fixture indices.
        vector[{0: 0, 1: 2, 2: 1}[int(np.argmax(color))]] = 1
        return vector.tolist()

    def image(self, path: Path):
        from PIL import Image

        self.calls += 1
        with Image.open(path) as image:
            color = np.asarray(image.convert("RGB")).mean(axis=(0, 1))
        vector = np.zeros(8)
        vector[{0: 0, 1: 2, 2: 1}[int(np.argmax(color))]] = 1
        return vector.tolist()


class FixtureAnalyzer:
    def __init__(self, embedder):
        self.embedder = embedder
        self.calls = 0

    def analyze(self, path, dialogue):
        self.calls += 1
        color = ["红", "蓝", "绿"][int(np.argmax(self.embedder.video(path)[:3]))]
        return Annotation(
            description=f"{color}色的测试画面", scene=["室内"], emotions=["平静"], shot_size=["近景"]
        )

    def group_scenes(self, units):
        return [list(range(len(units)))]


class FixtureWriter:
    """Tests only: deterministic writer, never a real model.

    By default the outline itself is the script, so existing tests that post a narration
    as the outline keep their storyboard text. ``vary=True`` appends a version marker so
    regeneration produces a different script each call.
    """

    def __init__(self, config: Config, *, vary: bool = False):
        self.config = config
        self.vary = vary
        self.calls = 0
        self.outlines: list[str] = []
        self.materials: list = []  # 每次调用收到的素材摘要（None 或 MaterialDigest）
        self.templates: list = []  # 每次调用收到的文案模板（ScriptTemplate）
        self.references: list = []  # 每次调用收到的参考文稿
        self.profiles: list = []  # 每次调用收到的账号档案写作约束（None 表示没有档案）

    def complete(self, instructions: str, text: str) -> str:
        return '{"title":"交付测试标题","description":"交付测试简介","tags":["测试"]}'

    def write(self, outline: str, *, template, materials=None, references=(), profile=None) -> str:
        self.calls += 1
        self.outlines.append(outline)
        self.materials.append(materials)
        self.templates.append(template)
        self.references.append(tuple(references))
        self.profiles.append(profile)
        # 大纲为空（改写模板）时以第一份参考文稿为文案。
        text = outline.strip() or next((r.strip() for r in references if r.strip()), "")
        return f"{text}第{self.calls}版。" if self.vary else text


@pytest.fixture(autouse=True)
def no_default_profiles(monkeypatch):
    """默认档案卡（GLM/MiniMax/ComfyUI）只在专门的用例里建：其余用例按空档案表断言。"""
    from settings import runtime_settings

    monkeypatch.setattr(runtime_settings, "ensure_default_profiles", lambda store: [])


@pytest.fixture(autouse=True)
def fixture_writer(monkeypatch):
    """Studio builds now start with script writing; never reach a real writer in tests."""
    from models import adapters

    monkeypatch.setattr(adapters, "make_writer", lambda config: FixtureWriter(config))


def convert_to_legacy(config: Config, job_id: str) -> dict:
    """Rewrite a freshly created job into the old shape: the text is the script (``input_text``).

    Jobs created before script writing exist on disk in this shape and keep working through
    the step state machine and the workspace migration; tests for those paths build them here.
    """
    from studio import studio

    state = studio.load_job(config, job_id)
    state["input_text"] = state.pop("outline")
    for key in ("delivery", "exports", "publish"):
        state["options"].pop(key, None)
    studio.save(studio.job_path(config, job_id), state)
    return state


def legacy_job(config: Config, text: str, **kwargs) -> dict:
    from studio import studio

    return convert_to_legacy(config, studio.create_job(config, text, **kwargs)["id"])


class FixtureStoryboarder:
    """Tests only: deterministic sentence-based split, never a real model."""

    def __init__(self, config: Config):
        self.config = config
        self.calls = 0
        self.vocabularies: list[list[str] | None] = []  # 每次 split 收到的 vocabulary，供测试断言
        self.suggestions: list[dict] = []  # 每次 suggest_query 收到的参数，供接口测试断言

    def title(self, text: str) -> str:
        return "测试创作标题"

    def suggest_query(
        self,
        text: str,
        visual: str,
        context: tuple[str, str],
        vocabulary: list[str] | None,
        *,
        language: str = "zh",
    ) -> str:
        """确定性地由输入派生召回词：visual 的前两个词 + “画面”，相同输入逐字节相同。"""
        self.suggestions.append(
            {
                "text": text,
                "visual": visual,
                "context": tuple(context),
                "vocabulary": None if vocabulary is None else list(vocabulary),
            }
        )
        words = (visual or "").split() or (text or "").split()
        head = " ".join(words[:2])[:40]
        return f"{head} 画面".strip()

    def split(
        self, text: str, vocabulary: list[str] | None = None, *, language: str = "zh"
    ) -> list[ShotDraft]:
        self.calls += 1
        self.vocabularies.append(None if vocabulary is None else list(vocabulary))
        parts: list[str] = []
        current = ""
        for char in text:
            current += char
            if (char in "。！？；\n" and len(current) >= 8) or len(current) >= 40:
                parts.append(current)
                current = ""
        if current.strip():
            parts.append(current)
        parts = [part.strip() for part in parts if part.strip()] or [text.strip()]
        return [
            ShotDraft(
                visual=f"画面：{part[:40]}",
                text=part,
                query=f"{part} 画面",
                estimated_seconds=round(len(part) / 4, 2),
            )
            for part in parts
        ]


class FixtureSpeaker:
    """Tests only: deterministic sine-wave wav bytes, never a real model."""

    def __init__(self, config: Config):
        self.config = config
        self.calls = 0

    def speak(self, text: str, voice: str | None = None) -> bytes:
        self.calls += 1
        duration = 0.3 + 0.12 * len(text)
        rate = 16000
        count = int(rate * duration)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            frames = bytearray()
            for index in range(count):
                frames += struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * index / rate)))
            handle.writeframes(bytes(frames))
        return buffer.getvalue()
