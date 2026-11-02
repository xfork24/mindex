#!/usr/bin/env python3
"""把 README 中同组截图合成为一张自动连播的动画图（动画 WebP，可回退 GIF）。

GitHub 渲染 README 时会剥离 ``<script>`` 与 ``style``，无法放置真正的轮播控件；
一组截图改为一张循环播放的动画图，配合 ``<details>`` 逐张原图清单，兼顾自动连播与
手动查看高清原图。分组、每帧秒数与标题在此文件维护，产物写到 ``docs/assets/``。

源图与产物的 SHA-256 记录在 ``docs/assets/gallery.json``：源图或参数变化后须重新生成，
``--check`` 据此判断产物是否过期（供测试调用）。

用法：
    python scripts/build-readme-gallery.py                     # 重新生成
    python scripts/build-readme-gallery.py --check             # 仅检查，过期时以退出码 1 结束
    python scripts/build-readme-gallery.py --format gif        # 回退为 GIF
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
MANIFEST = ASSETS / "gallery.json"

WIDTH = 1200
FRAME_MS = 3000
QUALITY = 82
METHOD = 6


@dataclass(frozen=True)
class Frame:
    file: str
    zh: str
    en: str


@dataclass(frozen=True)
class Group:
    key: str
    zh: str
    en: str
    frames: tuple[Frame, ...]


GROUPS: tuple[Group, ...] = (
    Group(
        key="admin",
        zh="镜头 AI 分析、背景音乐库与素材分组",
        en="Per-shot analysis, music library, and source groups",
        frames=(
            Frame("admin-shot-analysis.png", "镜头的 AI 分析结果", "Per-shot AI analysis result"),
            Frame("admin-music.png", "背景音乐曲库管理", "Background music library"),
            Frame("admin-groups.png", "素材分组管理", "Source group management"),
        ),
    ),
    Group(
        key="persons",
        zh="出镜识别与演员表导入",
        en="Appearance detection and cast import",
        frames=(
            Frame(
                "persons-appearance-detection.png",
                "出镜识别与逐镜证据审核",
                "Appearance detection with per-shot evidence review",
            ),
            Frame("persons-cast-import.png", "演员表导入与条目核对", "Cast-table import with entry review"),
        ),
    ),
    Group(
        key="search",
        zh="物体、场景、情绪与以图/以视频检索",
        en="Object, scene, emotion, and image/video queries",
        frames=(
            Frame("search-text-and-object.png", "物体与场景语义查询", "Semantic query for objects and scenes"),
            Frame("search-emotion-closeup.png", "情绪与景别查询", "Emotion and shot-type queries"),
            Frame("search-similar-scenes.png", "跨素材的相似场景检索", "Similar-scene lookup across sources"),
            Frame("search-by-image-or-video.png", "以图片或视频检索", "Image or video similarity search"),
        ),
    ),
    Group(
        key="studio",
        zh="创作任务与三类模板管理",
        en="Creation tasks and the three template families",
        frames=(
            Frame("studio-jobs.png", "创作任务列表", "Creation task list"),
            Frame("studio-script-templates.png", "文案模板管理", "Script template management"),
            Frame("studio-delivery-templates.png", "交付模板与平台规则管理", "Delivery template management"),
            Frame("studio-style-templates.png", "风格模板设置", "Style template settings"),
        ),
    ),
    Group(
        key="agent",
        zh="Agent 会话：文案、逐镜生产、成片预览与片段检索",
        en="Agent session: scripting, production, build preview, clip retrieval",
        frames=(
            Frame("agent-create-video-2.png", "Agent 会话：写文案并拆分镜", "Agent session: scripting and storyboard"),
            Frame(
                "agent-create-video-3.png",
                "Agent 会话：逐镜生产与配音",
                "Agent session: per-shot production and narration",
            ),
            Frame(
                "agent-create-video-4.png",
                "Agent 会话：最终构建与成片预览",
                "Agent session: final build with video preview",
            ),
            Frame("agent-search-clips.png", "Agent 检索特定视频片段", "Agent retrieving specific video clips"),
        ),
    ),
    Group(
        key="settings",
        zh="召回权重、模型档案与自定义配置",
        en="Recall weights, model profiles, and custom configuration",
        frames=(
            Frame(
                "settings-weights-voices-services.png",
                "召回权重、音色库、本地服务与工具管理",
                "Recall weights, voice library, local services, and tool management",
            ),
            Frame("settings-model-card.png", "模型档案卡", "Model profile card"),
            Frame("settings-model-config.png", "自定义模型档案配置", "Custom model profile configuration"),
        ),
    ),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output_path(group: Group, fmt: str) -> Path:
    return ASSETS / f"gallery-{group.key}.{fmt}"


def render(group: Group) -> list[Image.Image]:
    """统一画布为组内最宽帧的宽高比；每帧等比缩放后白底居中，保证各帧尺寸一致。"""
    loaded: list[Image.Image] = []
    for frame in group.frames:
        source = ASSETS / frame.file
        if not source.is_file():
            raise FileNotFoundError(f"缺少源图：{source.relative_to(ROOT)}")
        loaded.append(Image.open(source).convert("RGB"))
    aspect = max(image.width / image.height for image in loaded)
    height = round(WIDTH / aspect)
    canvas: list[Image.Image] = []
    for image in loaded:
        scale = min(WIDTH / image.width, height / image.height)
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        thumb = image.resize(size, Image.LANCZOS)
        target = Image.new("RGB", (WIDTH, height), "white")
        target.paste(thumb, ((WIDTH - size[0]) // 2, (height - size[1]) // 2))
        canvas.append(target)
    return canvas


def build(fmt: str) -> dict:
    payload: dict = {"width": WIDTH, "frame_ms": FRAME_MS, "format": fmt, "groups": []}
    for group in GROUPS:
        canvas = render(group)
        target = output_path(group, fmt)
        if fmt == "webp":
            canvas[0].save(
                target,
                save_all=True,
                append_images=canvas[1:],
                duration=FRAME_MS,
                loop=0,
                quality=QUALITY,
                method=METHOD,
            )
        else:
            canvas[0].save(
                target,
                save_all=True,
                append_images=canvas[1:],
                duration=FRAME_MS,
                loop=0,
                optimize=True,
                disposal=2,
            )
        payload["groups"].append(
            {
                "key": group.key,
                "output": target.name,
                "output_sha256": sha256(target),
                "sources": [
                    {"file": frame.file, "zh": frame.zh, "en": frame.en, "sha256": sha256(ASSETS / frame.file)}
                    for frame in group.frames
                ],
            }
        )
        print(f"已生成 {target.relative_to(ROOT)}（{target.stat().st_size // 1024} KB）")
    MANIFEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def check(fmt: str) -> list[str]:
    if not MANIFEST.is_file():
        return [f"{MANIFEST.relative_to(ROOT)} 不存在，请先运行本脚本生成"]
    recorded = json.loads(MANIFEST.read_text(encoding="utf-8"))
    problems: list[str] = []
    if (recorded.get("width"), recorded.get("frame_ms"), recorded.get("format")) != (WIDTH, FRAME_MS, fmt):
        problems.append("生成参数已变（宽度/帧时长/格式），请重新运行本脚本")
    groups = {item["key"]: item for item in recorded.get("groups", [])}
    for group in GROUPS:
        item = groups.get(group.key)
        if item is None:
            problems.append(f"{group.key}：清单中缺少该分组")
            continue
        target = ASSETS / item["output"]
        if not target.is_file():
            problems.append(f"{group.key}：产物 {item['output']} 不存在")
        elif sha256(target) != item["output_sha256"]:
            problems.append(f"{group.key}：产物 {item['output']} 与清单不一致")
        if len(item["sources"]) != len(group.frames):
            problems.append(f"{group.key}：分组帧数与清单不一致")
            continue
        for frame, source in zip(group.frames, item["sources"]):
            if source["file"] != frame.file:
                problems.append(f"{group.key}：帧顺序与清单不一致（{frame.file}）")
            elif not (ASSETS / frame.file).is_file():
                problems.append(f"{group.key}：源图 {frame.file} 不存在")
            elif sha256(ASSETS / frame.file) != source["sha256"]:
                problems.append(f"{group.key}：源图 {frame.file} 已变化，请重新生成")
    extra = set(groups) - {group.key for group in GROUPS}
    for key in sorted(extra):
        problems.append(f"{key}：清单中存在已移除的分组产物 {groups[key]['output']}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 README 自动连播截图并检查产物是否过期。")
    parser.add_argument("--check", action="store_true", help="只检查，不写文件")
    parser.add_argument("--format", choices=("webp", "gif"), default="webp", help="产物格式（默认 webp）")
    args = parser.parse_args(argv)

    if args.check:
        problems = check(args.format)
        for problem in problems:
            print(problem, file=sys.stderr)
        print("README 截图产物已同步" if not problems else f"发现 {len(problems)} 处问题", file=sys.stderr)
        return 0 if not problems else 1
    build(args.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
