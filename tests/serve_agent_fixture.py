"""智能助手界面验证专用：规则化的确定性模型 + 带分镜的创作任务，绝不代表真实模型质量。

用法：python tests/serve_agent_fixture.py --data-dir /tmp/agent-ui --port 18342
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from conftest import FixtureEmbedder, FixtureSpeaker, FixtureStoryboarder, legacy_job

from mindex.agent.llm.scripted import ScriptedLlm, call, say
from mindex.agent.llm.types import message_text
from mindex.api import create_app
from mindex.shared.config import Config
from mindex.studio import studio
from mindex.studio import studio_workspace as ws

SHOTS = ["shot-a1daad59aa", "shot-925281d5bb", "shot-77f0c3e1cc"]


class FixtureAgentLlm(ScriptedLlm):
    """脚本为空时按规则回应：读取最后一条消息决定调用工具或回答。"""

    job_id: str | None = None
    segment_id: str | None = None

    async def stream(self, request):
        if not self.script:
            self.script.append(self.rule(request))
        async for chunk in super().stream(request):
            yield chunk

    def rule(self, request):
        last = request.messages[-1]
        if last["role"] == "tool":
            text = message_text(last)
            if text.startswith("Error:"):
                return [say("操作没有完成：" + text[6:].strip()[:120])]
            first = text.splitlines()[0] if text else ""
            count = re.search(r"分镜 (\d+) 个", text)
            if count:
                return [say(f"当前方案共有 {count.group(1)} 个分镜。")]
            return [say("已完成：" + first[:120])]
        user = message_text(last)
        if "智能助手角色" in user:
            return [call("navigate", page="settings", role="agent")]
        second = re.search(r"查看第\s*(\d+)\s*镜", user)
        if second:
            return [call("resolve_shots", job_id=self.job_id, refs=[f"#{second.group(1)}"])]
        if "列出所有分镜" in user:
            return [say("## 分镜\n\n| # | 旁白 |\n|---|---|\n| 1 | 红色开场 |\n| 2 | **蓝色**中段 |\n\n"
                        "```text\nshot-a1daad59\n```\n\n<script>window.__xss = 1</script>\n\n"
                        "[危险](javascript:alert(1)) [文档](https://example.com/docs)")]
        if "的媒体" in user:
            return [call("resolve_shots", job_id=self.job_id, refs=["#1"])]
        if "看成片" in user:
            return [call("get_workspace_summary", job_id=self.job_id)]
        if "找绿色素材" in user:
            return [call("search_media", text="绿色 画面", limit=3)]
        if "显示参考图" in user:
            return [say(f"本站图：![镜头缩略图](/api/admin/segments/{self.segment_id}/thumbnail)\n\n"
                        "外链图：![外链](https://example.com/tracker.png)")]
        if "几个分镜" in user:
            return [say("我先读取工作台。"), call("get_workspace_summary")]
        merge = re.search(r"合并\s*([0-9a-f]{6,})\s*和\s*([0-9a-f]{6,})", user)
        if merge:
            return [call("edit_shots", job_id=self.job_id,
                         operations=[{"op": "merge", "shots": [merge.group(1), merge.group(2)]}])]
        if "列出" in user:
            return [call("list_jobs")]
        return [say(f"收到：{user[:60]}")]


def seed(config: Config) -> str:
    job_id = legacy_job(config, "红色开场。蓝色中段。绿色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(config, job_id, w, p, [
        studio.Shot(id=sid, text=text, query=text[:2], estimated_seconds=1.5)
        for sid, text in zip(SHOTS, ["红色开场。", "蓝色中段。", "绿色结尾。"])
    ])
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return job_id


def build_media(config: Config, job_id: str):
    """真实素材、索引与一次完整构建：让助手工具结果带有缩略图、配音、渲染视频与成片。"""
    from mindex.processing.indexing import IndexBuilder
    from mindex.processing.pipeline import Pipeline
    from mindex.shared.media import command
    from mindex.shared.storage import Store

    video = config.data_dir / "工程测试专用_颜色样本.mp4"
    command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=c=red:s=160x120:r=25:d=1",
             "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=25:d=1",
             "-f", "lavfi", "-i", "color=c=green:s=160x120:r=25:d=8",
             "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)])
    config.studio.render.width, config.studio.render.height = 160, 120
    config.studio.render.preset = "ultrafast"
    Pipeline(config).prepare(video)
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot", "moment"), activate=True)
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    checked = ws.preflight(config, job_id, w, p)
    w["builds"].append({"id": "seedbuild001", "plan_id": p["id"], "status": "running", "force": False,
                        "created_at": studio.now(), "nodes": [{**n, "status": "pending"} for n in checked["nodes"]]})
    ws.persist(config, job_id, w)
    ws.run_build(config, job_id, "seedbuild001", speaker=FixtureSpeaker(config), embedder=embedder)
    segment = Store(config.data_dir).segments(kind="shot")[0]["id"]
    return embedder, segment


def build_app(data_dir: Path, *, media: bool = False):
    config = Config(data_dir=data_dir.resolve())
    config.embedding.dimensions = 8
    config.data_dir.mkdir(parents=True, exist_ok=True)
    job_id = seed(config)
    llm = FixtureAgentLlm()
    llm.job_id = job_id
    embedder = None
    if media:
        embedder, llm.segment_id = build_media(config, job_id)
    app = create_app(config, embedder, storyboarder=FixtureStoryboarder(config), speaker=FixtureSpeaker(config),
                     agent_llm=llm)
    return app, job_id


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18342)
    args = parser.parse_args()
    app, job_id = build_app(args.data_dir)
    print(json.dumps({"job_id": job_id}), flush=True)
    print("TEST ONLY: rule-based fixture assistant; no real model quality is evaluated.")
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
