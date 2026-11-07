"""智能助手验收场景：同一组场景既用脚本化模型做确定性测试，也由 scripts/agent_eval.py 交给真实模型评测。

每个场景给出：用户请求、页面上下文、确认与提问的自动答复策略、按最终状态判定的检查函数，
以及确定性测试用的脚本（真实模型评测时忽略脚本）。检查只看结果状态，不要求固定的调用顺序。
场景可带 ``setup``（在公共数据之上准备专属数据）与 ``attachments``（发送前以面板身份上传的附件）；
``prepare`` 统一完成这两步，确定性测试与真实模型评测共用。
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image

from agent.llm.scripted import call, say
from agent.runtime import NO_VISION_PROMPT, NO_VISION_TOOL_NOTE
from api import create_app
from conftest import FixtureEmbedder, FixtureSpeaker, FixtureStoryboarder, FixtureWriter, legacy_job
from models.video_generators import VideoStatus, allowed_seconds
from processing.indexing import IndexBuilder
from processing.pipeline import Pipeline
from shared.config import Config
from shared.storage import Store, dumps
from studio import studio
from studio import workspace as ws

SHOTS = ["shot-a1daad59aa", "shot-925281d5bb", "shot-925281d6cc", "shot-77f0c3e1dd"]
SLOW_SHOTS = ["shot-5105105a01", "shot-5105105a02"]
SECRET = "sk-scenario-PLAINTEXT-987654321zz"
ATTACHMENT = re.compile(r"attachment://[A-Za-z0-9_-]+")
# 慢速配音最多等待取消请求的时长：取消场景里保证「开始的节点照常完成、其余节点被取消」可确定复现。
SLOW_HOLD_SECONDS = 60


class FlakySpeaker(FixtureSpeaker):
    """测试专用：旁白含「故障」时模拟配音服务不可用；含「慢速」时一直等到有构建被请求取消（或超时）再返回。"""

    def speak(self, text, voice=None):
        if "故障" in text:
            raise RuntimeError("TTS 服务不可用：连接被拒绝")
        if "慢速" in text:
            jobs = studio.studio_root(self.config)
            deadline = time.monotonic() + SLOW_HOLD_SECONDS
            while time.monotonic() < deadline and not any(jobs.glob("*/builds/*.cancel")):
                time.sleep(0.05)
        return super().speak(text, voice)


class ScenarioVideoGenerator:
    """测试专用：确定性视频生成服务（本机 ffmpeg 生成紫色片段，排队 → 生成中 → 完成），绝不调用付费接口。"""

    def __init__(self):
        self.requests, self.cancelled, self.current = [], [], 0

    def submit(self, request):
        self.requests.append(request)
        self.current = 0
        return f"scenario-task-{len(self.requests)}"

    def poll(self, task_id):
        state = ("queued", "running", "succeeded")[min(self.current, 2)]
        self.current += 1
        return VideoStatus(
            state=state,
            url="https://cdn.example.invalid/out.mp4" if state == "succeeded" else None,
            raw={"task_id": task_id, "status": state},
        )

    def fetch(self, status, target: Path):
        seconds = self.requests[-1].seconds
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=purple:s=160x120:r=25:d={seconds}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-f",
                "mp4",
                str(target),
            ],
            check=True,
        )

    def cancel(self, task_id):
        self.cancelled.append(task_id)

    def health(self):
        return {"backend": "scenario", "ready": True, "reply": "测试桩（未发起生成）"}

    def allowed_seconds(self, seconds):
        return allowed_seconds([1.0, 2.0, 3.0], seconds)


class ScenarioImageGenerator:
    """测试专用：确定性图片生成服务（返回绿色 PNG），绝不调用付费接口。"""

    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return png("green")

    def health(self):
        return {"backend": "scenario", "ready": True, "reply": "测试桩（未生成图片）"}


class ScenarioMusicGenerator:
    """测试专用：确定性音乐生成与歌词生成（本机 ffmpeg 生成正弦波），绝不调用付费接口。"""

    def __init__(self):
        self.requests, self.lyric_calls = [], []

    def generate(self, request):
        self.requests.append(request)
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=1", "-f", "wav", "-"],
            check=True,
            capture_output=True,
        ).stdout
        return out, {"music_duration": 1000}

    def lyrics(self, prompt, *, mode="write_full_song", lyrics=None, title=None):
        from models.music_generators import Lyrics

        self.lyric_calls.append(prompt)
        return Lyrics(
            title=title or "极光", style_tags="Pop, Dreamy", lyrics=f"[Verse]\n{prompt}\n[Chorus]\n啦啦啦"
        )

    def health(self):
        return {"backend": "scenario", "ready": True, "reply": "测试桩（未生成音乐）"}


SCENARIO_BAR = "#1f77b4"


class ScenarioAnimator:
    """测试专用：确定性动画生成模型（返回固定柱状图 HTML），绝不调用真实 LLM。"""

    def __init__(self):
        self.calls = []

    def animate(self, brief, *, seconds, width, height, fps, previous=None, feedback=None):
        self.calls.append({"brief": brief, "seconds": seconds, "width": width, "height": height})
        bars = "".join(f'<div class="bar" style="height:{20 + 15 * i}px"></div>' for i in range(5))
        return (
            "<!DOCTYPE html><html><head><style>html,body{margin:0;overflow:hidden;background:#fff;"
            "font-family:sans-serif}.bar{display:inline-block;width:20px;margin:2px;background:"
            f"{SCENARIO_BAR}"
            + "}</style></head><body>"
            + bars
            + "<script>requestAnimationFrame(function f(t){document.body.style.opacity=Math.min(1,t/300);"
            "requestAnimationFrame(f)})</script></body></html>"
        )

    def ping(self):
        return {"model": "scenario", "reply": "ok"}


def scenario_capture(html, *, seconds, width, height, fps, target, max_seconds, frame_timeout=5, frames=None):
    """测试专用：代替 Chromium 沙箱录制（ffmpeg 纯色画面，帧数精确），
    确定性测试与真实模型评测都不需要浏览器。
    """
    color = "orange" if "#ff8800" in html else "blue"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={width}x{height}:r={fps}",
            "-frames:v",
            str(frames),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "mp4",
            str(target),
        ],
        check=True,
    )
    return {"frames": frames, "fps": str(fps), "blocked_requests": [], "wall_seconds": 0.0}


def png(color: str) -> bytes:

    buffer = io.BytesIO()
    Image.new("RGB", (96, 64), color).save(buffer, "PNG")
    return buffer.getvalue()


@dataclass
class World:
    config: Config
    embedder: Any
    job: str
    plan: str
    other_plan: str
    broken_job: str
    broken_build: str
    source_id: str = ""
    extra: dict = field(default_factory=dict)  # 场景 setup 准备的专属数据
    # 视频生成始终注入确定性桩：确定性测试与真实模型评测都不会调用付费生成接口。
    video_generator: ScenarioVideoGenerator = field(default_factory=ScenarioVideoGenerator)
    # 动画生成与录制同样注入确定性替身：不调用真实 LLM，也不需要 Chromium。
    animator: ScenarioAnimator = field(default_factory=ScenarioAnimator)
    image_generator: ScenarioImageGenerator = field(default_factory=ScenarioImageGenerator)
    music_generator: ScenarioMusicGenerator = field(default_factory=ScenarioMusicGenerator)

    def app(self, llm=None):
        app = create_app(
            self.config,
            self.embedder,
            storyboarder=FixtureStoryboarder(self.config),
            speaker=FlakySpeaker(self.config),
            writer=FixtureWriter(self.config),
            agent_llm=llm,
            video_generator=self.video_generator,
            animator=self.animator,
            animation_capture=scenario_capture,
            image_generator=self.image_generator,
            music_generator=self.music_generator,
        )
        app.state.agent_runtime.watcher.interval = 0.05
        return app


def board(config, job_id, texts, ids):
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(
        config,
        job_id,
        w,
        p,
        [
            studio.Shot(id=sid, text=text, query=text[:2], estimated_seconds=1.0)
            for sid, text in zip(ids, texts)
        ],
    )
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return p["id"]


def build_world(config: Config, video: Path) -> World:
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.preset = "ultrafast"
    # 嵌入身份探测（设置页嵌入状态、激活预览）走真实适配器：
    # 指向本机关闭端口，立即失败而不是等待默认局域网地址超时。
    config.embedding.base_url = "http://127.0.0.1:9"
    config.embedding.timeout = 5
    source_id = Pipeline(config).prepare(video)
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot", "moment"), activate=True)
    job = legacy_job(
        config, "红色开场。蓝色中段。蓝色延续。绿色结尾。", options={"burn": False}, title="颜色短片"
    )["id"]
    plan = board(config, job, ["红色开场。", "蓝色中段。", "蓝色延续。", "绿色结尾。"], SHOTS)
    broken = legacy_job(config, "红色开场。配音故障。", options={"burn": False}, title="故障任务")["id"]
    board(config, broken, ["红色开场。", "配音故障。"], ["shot-0b0b0b0b0a", "shot-0b0b0b0b0b"])
    world = World(config, embedder, job, plan, "", broken, "", source_id)

    with TestClient(world.app()) as client:
        data = client.get(f"/api/studio/jobs/{job}/workspace").json()
        other = client.post(
            f"/api/studio/jobs/{job}/plans", json={"name": "备选方案", "copy_from": plan}
        ).json()
        world.other_plan = other["active_plan"]
        client.post(f"/api/studio/jobs/{job}/plans/{plan}/activate")
        bdata = client.get(f"/api/studio/jobs/{broken}/workspace").json()
        token = client.get(f"/api/studio/jobs/{broken}/plans/{bdata['active_plan']}/preflight").json()[
            "token"
        ]
        build = client.post(
            f"/api/studio/jobs/{broken}/plans/{bdata['active_plan']}/build", json={"token": token}
        ).json()
        deadline = time.monotonic() + 120
        while (
            client.get(f"/api/studio/jobs/{broken}/builds/{build['id']}/logs").json()["status"] == "running"
        ):
            assert time.monotonic() < deadline, "故障任务构建未结束"
            time.sleep(0.1)
        world.broken_build = build["id"]
        assert data["job"]["id"] == job
    return world


def workspace(client, job):
    return client.get(f"/api/studio/jobs/{job}/workspace").json()


def order(data, plan_id=None):
    p = data["plans"][plan_id or data["active_plan"]]
    return data["versions"][p["board"]]["data"]["order"] if p["board"] else []


def final_text(events):
    messages = [e["data"]["message"] for e in events if e["type"] == "assistant/message"]
    return "\n".join(b.get("text", "") for m in messages for b in m["content"] if b["type"] == "text")


def tool_names(events):
    return [e["data"]["name"] for e in events if e["type"] == "tool/call"]


def last_tool(request) -> str:
    return request.messages[-1]["content"][0]["text"]


def attachment_ref(request) -> str:
    """请求里用户附件行中的第一个 attachment:// 引用（面板发送消息时由运行时追加）。"""
    for message in request.messages:
        for block in message["content"]:
            hit = ATTACHMENT.search(block.get("text") or "") if block.get("type") == "text" else None
            if hit:
                return hit.group(0)
    raise AssertionError("请求中没有附件引用")


def calls_of(events, name) -> list[dict]:
    out = []
    for e in events:
        if e["type"] == "tool/call" and e["data"]["name"] == name:
            raw = e["data"]["arguments"]
            out.append(json.loads(raw or "{}") if isinstance(raw, str) else dict(raw or {}))
    return out


def results_of(events, name) -> list[dict]:
    """某个工具的全部结果事件数据（按调用 ID 对应）。"""
    ids = {e["data"]["callId"] for e in events if e["type"] == "tool/call" and e["data"]["name"] == name}
    return [
        e["data"] for e in events if e["type"] == "tool/result" and e["data"]["message"]["toolCallId"] in ids
    ]


def ok_results(events, name) -> list[dict]:
    return [r for r in results_of(events, name) if "error" not in r]


def text_of(result: dict) -> str:
    return "".join(b.get("text", "") for b in result["message"]["content"] if b["type"] == "text")


def followups(events) -> list[dict]:
    return [e for e in events if e["type"] == "user/message" and e["data"]["source"]["kind"] == "followup"]


def scripted_requests(client) -> list | None:
    """确定性测试里脚本化模型收到的全部请求；真实模型评测时为 None。"""
    llm = client.app.state.agent_runtime.llm_factory()
    return llm.requests if getattr(llm, "provider", None) == "scripted" else None


def image_blocks(request) -> list[dict]:
    """请求中已解析为图片数据的块（只含引用、未解析的块不算）。"""
    return [b for m in request.messages for b in m["content"] if b["type"] == "image" and b.get("data")]


@dataclass
class Scenario:
    name: str
    prompt: str
    check: Any
    script: Any = None  # (world) -> list，确定性测试用
    page: Any = None  # (world) -> dict
    approve: str = "allow"  # allow / reject
    form: dict = field(default_factory=dict)  # 字段关键词 -> 值
    choose: str | None = None  # 选项问题优先选择包含此文本的选项
    setup: Any = None  # (world) -> None：公共数据建好后、创建应用前准备专属数据
    attachments: Any = None  # (world) -> [(文件名, 内容, 内容类型)]：随消息发送的面板附件
    note: str = ""  # 场景说明（评测报告与 --list 使用）
    needs: tuple[str, ...] = ()  # 对被评测模型的额外要求，例如 ("vision",) 需要支持图片输入


# ------------------------------------------------------------ 检查函数
def check_custom_model(world, client, events):
    settings = client.get("/api/settings?probe_embedding=false").json()
    generator = next(r for r in settings["roles"] if r["role"] == "generator")
    created = [p for p in generator["profiles"] if p["config"].get("base_url") == "http://127.0.0.1:9/v1"]
    assert created, "没有新建指向自建 endpoint 的拆分镜档案"
    assert created[0]["api_key"]["has_key"], "档案没有通过表单拿到密钥"
    assert generator["active_profile_id"] == created[0]["id"], "新档案未激活"
    assert "ask_user_question" in tool_names(events), "没有向用户澄清连接信息"


def check_gap(world, client, events):
    assert client.get("/api/agent/gaps").json()["items"], "没有记录能力缺口"
    assert order(workspace(client, world.job)) == SHOTS, "不应修改分镜"
    assert not any(n in tool_names(events) for n in ("edit_shots", "run_build", "run_node"))


def check_merge_build(world, client, events):
    data = workspace(client, world.job)
    shots = order(data)
    assert len(shots) == 3 and SHOTS[2] not in shots, f"合并结果不对：{shots}"
    merged = data["versions"][data["plans"][data["active_plan"]]["shots"][SHOTS[1]]["content"]]["data"]
    assert merged["text"] == "蓝色中段。蓝色延续。" and merged["query"] == "蓝色；蓝色"
    finished = [e["data"] for e in events if e["type"] == "plugin:mindex/build-finished"]
    assert finished and finished[-1]["status"] == "complete", f"构建未完成：{finished}"
    assert any(e["type"] == "user/message" and e["data"]["source"]["kind"] == "followup" for e in events)


def check_switch_plan(world, client, events):
    assert workspace(client, world.job)["active_plan"] == world.other_plan


def check_voice(world, client, events):
    options = workspace(client, world.job)["plans"][world.plan]["options"]
    assert options["voice"] == "Ethan" and options["voice_mode"] == "custom", options


def check_diagnose(world, client, events):
    assert "get_build_status" in tool_names(events)
    assert "不可用" in final_text(events) or "TTS" in final_text(events), "没有说明失败原因"


def check_search(world, client, events):
    results = [
        e
        for e in events
        if e["type"] == "tool/result" and (e["data"].get("meta") or {}).get("kind") == "search"
    ]
    assert results and results[-1]["data"]["meta"]["count"] > 0


def check_ambiguous(world, client, events):
    assert "ask_user_question" in tool_names(events), "短 ID 有歧义时必须向用户确认"
    data = workspace(client, world.job)
    shots = order(data)
    assert SHOTS[1] not in shots and SHOTS[2] in shots and len(shots) == 3, shots


def check_rejected(world, client, events):
    assert order(workspace(client, world.job)) == SHOTS
    decided = [e["data"]["outcome"] for e in events if e["type"] == "plugin:mindex/approval-decided"]
    assert decided and set(decided) == {"rejected"}
    assert client.get(f"/api/studio/jobs/{world.job}").status_code == 200


def check_secret(world, client, events):
    check_custom_model(world, client, events)
    sid = events[0]["session_id"]
    raw = client.get(f"/api/agent/sessions/{sid}/events", params={"follow": "false"}).text
    assert SECRET not in raw and SECRET not in json.dumps(events, ensure_ascii=False)
    llm = client.app.state.agent_runtime.llm_factory()
    if hasattr(llm, "requests"):
        assert SECRET not in json.dumps([r.messages for r in llm.requests], ensure_ascii=False, default=str)


# ------------------------------------------------------------ 确定性脚本
def custom_model_script(world):
    ask = call(
        "ask_user_question",
        questions=[
            {
                "id": "conn",
                "question": "请提供自建模型的连接信息",
                "fields": [
                    {"id": "base_url", "label": "Endpoint", "required": True},
                    {"id": "model", "label": "模型名", "required": True},
                    {"id": "api_key", "label": "API Key", "type": "secret"},
                ],
            }
        ],
    )

    def create(request):
        values = json.loads(last_tool(request))["answers"][0]["values"]
        return [
            call(
                "create_model_profile",
                role="generator",
                name="自建拆分镜",
                provider="self",
                config={
                    "backend": "openai",
                    "base_url": values["base_url"],
                    "model": values["model"],
                    "llama_template": True,
                },
                api_key=values["api_key"],
            )
        ]

    def activate(request):
        profile = int(re.search(r"档案 #(\d+)", last_tool(request)).group(1))
        return [call("activate_model_profile", profile_id=profile)]

    # 未给页面上下文：先加载 settings 领域，再使用设置工具。
    return [
        [call("load_tools", domains=["settings"])],
        [call("list_provider_presets", role="generator"), ask],
        create,
        activate,
        [say("已切换到自建模型。")],
    ]


def merge_script(world):
    return [
        [call("resolve_shots", job_id=world.job, refs=["925281d5bb", "925281d6"])],
        [
            call(
                "edit_shots",
                job_id=world.job,
                operations=[{"op": "merge", "shots": ["925281d5bb", "925281d6"]}],
            )
        ],
        [call("run_build", job_id=world.job)],
        [say("已提交构建。")],
        [say("构建完成，合并后的分镜已重建，其余复用。")],
    ]


def ambiguous_script(world):
    ask = call(
        "ask_user_question",
        questions=[
            {
                "id": "which",
                "question": "925281d 对应两个分镜，要删除哪一个？",
                "options": [{"label": "#2 shot-925281d5"}, {"label": "#3 shot-925281d6"}],
            }
        ],
    )

    def delete(request):
        selected = json.loads(last_tool(request))["answers"][0]["selected"][0]
        return [
            call("edit_shots", job_id=world.job, operations=[{"op": "delete", "shot": selected.split()[0]}])
        ]

    return [[call("resolve_shots", job_id=world.job, refs=["925281d"])], [ask], delete, [say("已删除。")]]


# ------------------------------------------------------------ 能力扩展场景：准备数据
def setup_index_versions(world):
    """在初始索引之上再建一个更新的活动版本，供回退。"""
    store = Store(world.config.data_dir)
    older = store.active_index()["id"]
    newer = IndexBuilder(world.config, world.embedder).build(
        source_id=world.source_id, channels=["visual"], activate=True
    )
    assert newer != older and store.active_index()["id"] == newer
    world.extra.update(older=older, newer=newer)


def setup_slow_job(world):
    """第二个分镜的配音会一直等到有人请求取消构建，保证取消落在执行中途。"""
    job = legacy_job(world.config, "红色开场。慢速配音。", options={"burn": False}, title="慢速任务")["id"]
    world.extra.update(
        slow_job=job, slow_plan=board(world.config, job, ["红色开场。", "慢速配音。"], SLOW_SHOTS)
    )


def setup_gpu_busy(world):
    """模拟另一个 GPU 任务独占 pipeline 锁：发布任务停在 GPU 准入等待（queued），可被确定地取消。"""
    from shared.locks import SharedFileLock

    world.extra["gpu_holder"] = SharedFileLock(
        world.config.data_dir / "pipeline.lock", exclusive=True
    ).acquire()
    world.extra["index_before"] = Store(world.config.data_dir).active_index()["id"]


def setup_persons(world):
    """一部作品、一名演员与其角色，以及首个镜头上一条待确认的出镜证据。"""
    from persons.person_models import Entity
    from persons.persons import Persons

    store = Store(world.config.data_dir)
    persons = Persons(store)
    work = persons.work_for(world.source_id)
    actor = persons.save(Entity(kind="actor", name="演员甲"))["id"]
    role = persons.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor]))["id"]
    active = store.active_index()
    shot = min((s for s in store.snapshots(active["id"]) if s["kind"] == "shot"), key=lambda s: s["start"])
    store.execute(
        "INSERT INTO person_evidence(id,source_id,shot_id,actor_id,role_id,start,end,status,data) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "ev-scenario",
            world.source_id,
            shot["id"],
            actor,
            role,
            shot["start"],
            shot["end"],
            "candidate",
            dumps({"score": 0.82}),
        ),
    )
    world.extra.update(actor=actor, role=role, shot=shot["id"], release_before=active["search_release_id"])


def setup_vision(world):
    world.config.agent.llm.vision = True
    world.config.agent.max_images = 8


def red_attachment(world):
    return [("红色截图.png", png("red"), "image/png")]


def admin_page(world):
    return {"page": "admin", "source_id": world.source_id}


# ------------------------------------------------------------ 能力扩展场景：检查函数
def check_cross_domain(world, client, events):
    sid = events[0]["session_id"]
    loaded = client.app.state.agent_runtime.loaded_domains(sid)
    # 页面领域 studio 最先加载，随后加载 search / index / persons：最久未用的 studio 被淘汰，始终不超过 3 个。
    assert loaded and len(loaded) <= 3 and "meta" not in loaded, loaded
    assert {"search", "index", "persons"} <= set(loaded) and "studio" not in loaded, loaded
    for event in events:
        if event["type"] == "plugin:mindex/tools-loaded":
            assert len(event["data"]["loaded"]) <= 3, event["data"]
    search = ok_results(events, "search_media")
    assert search and search[-1]["meta"]["count"] > 0, "没有检索到素材"
    assert ok_results(events, "list_index_versions") and ok_results(events, "list_persons")
    # TOOL_NOT_LOADED 是可恢复错误：之后同名工具必须成功执行过。
    names = {e["data"]["callId"]: e["data"]["name"] for e in events if e["type"] == "tool/call"}
    seq = {e["data"]["message"]["toolCallId"]: e["seq"] for e in events if e["type"] == "tool/result"}
    misses = [
        e
        for e in events
        if e["type"] == "tool/result" and (e["data"].get("error") or {}).get("code") == "TOOL_NOT_LOADED"
    ]
    for miss in misses:
        name = names[miss["data"]["message"]["toolCallId"]]
        assert "load_tools" in text_of(miss["data"])
        assert any(seq[r["message"]["toolCallId"]] > miss["seq"] for r in ok_results(events, name)), name
    requests = scripted_requests(client)
    if requests is not None:
        assert misses, "脚本应先触发一次 TOOL_NOT_LOADED"
        offered = {t["name"] for t in requests[-1].tools}
        assert {"load_tools", "search_media", "list_index_versions", "list_persons"} <= offered
        assert "run_build" not in offered, "被淘汰领域的工具不应再下发"


def check_attachment_search(world, client, events):
    user = next(
        e["data"] for e in events if e["type"] == "user/message" and e["data"]["source"]["kind"] == "user"
    )
    refs = [item["ref"] for item in user.get("attachments") or []]
    assert len(refs) == 1 and refs[0].startswith("attachment://"), user.get("attachments")
    assert refs[0] in [c.get("attachment") for c in calls_of(events, "search_by_media")], "没有用附件检索"
    found = ok_results(events, "search_by_media")
    assert found and found[-1]["meta"]["count"] > 0, "以图检索没有结果"
    first = text_of(found[-1]).splitlines()[0]
    assert first.startswith("#1") and " 0.0–" in first, f"红色截图应命中红色开场：{first}"


def check_similar(world, client, events):
    search = ok_results(events, "search_media")
    assert search and search[-1]["meta"]["count"] > 0
    listed = {sid for r in search for sid in re.findall(r"^#\d+ .* 片段 (\S+)", text_of(r), re.M)}
    seeds = [c["segment_id"] for c in calls_of(events, "find_similar")]
    assert seeds and seeds[-1] in listed, f"种子片段应来自检索结果：{seeds} / {listed}"
    similar = ok_results(events, "find_similar")
    assert similar and similar[-1]["meta"]["count"] > 0, "找相似没有结果"
    text = text_of(similar[-1])
    assert text.startswith(f"与片段 {seeds[-1]} 画面相似的片段")
    assert seeds[-1] not in re.findall(r"^#\d+ .* 片段 (\S+)", text, re.M), "结果不应包含种子片段"


def check_index_rollback(world, client, events):
    assert Store(world.config.data_dir).active_index()["id"] == world.extra["older"], "活动索引没有回退"
    assert ok_results(events, "list_index_versions") and ok_results(events, "check_index")
    assert ok_results(events, "activate_index"), "没有执行激活"
    asked = [e["data"] for e in events if e["type"] == "plugin:mindex/approval-asked"]
    assert any(a["name"] == "activate_index" and a["risk"] == "danger" for a in asked), "激活必须经确认卡"
    mutations = [e["data"] for e in events if e["type"] == "plugin:mindex/mutation"]
    assert {"domain": "settings", "index_id": world.extra["older"]} in mutations, mutations


def check_cancel_build(world, client, events):
    job = world.extra["slow_job"]
    build = workspace(client, job)["builds"][-1]
    assert build["status"] == "cancelled", f"构建状态：{build['status']}"
    cancelled = [n for n in build["nodes"] if n.get("status") == "blocked" and n.get("error") == "已取消"]
    assert cancelled, "未开始的节点应标为已取消"
    assert ok_results(events, "run_build") and ok_results(events, "cancel_build")
    finished = [e["data"] for e in events if e["type"] == "plugin:mindex/build-finished"]
    assert finished and finished[-1]["status"] == "cancelled" and finished[-1]["build_id"] == build["id"], (
        finished
    )
    assert followups(events), "构建结束后应有 followup 总结"
    assert not any(studio.job_dir(world.config, job).glob("builds/*.cancel")), "取消哨兵应在构建结束后清理"


def check_cancel_task(world, client, events):
    try:
        started = ok_results(events, "start_source_task")
        assert started, "没有发起素材任务"
        task_id = started[-1]["meta"]["task_id"]
        assert ok_results(events, "cancel_task"), "没有取消任务"
        task = client.get(f"/api/admin/source-tasks/{task_id}").json()
        assert task["status"] == "cancelled", task
        finished = [e["data"] for e in events if e["type"] == "plugin:mindex/task-finished"]
        assert finished and finished[-1]["task_id"] == task_id and finished[-1]["status"] == "cancelled", (
            finished
        )
        texts = [e["data"]["content"][0]["text"] for e in followups(events)]
        assert any(f"素材任务 {task_id}" in t and "cancelled" in t for t in texts), texts
        # 发布在 GPU 准入前被取消：活动索引不变。
        assert Store(world.config.data_dir).active_index()["id"] == world.extra["index_before"]
    finally:
        world.extra["gpu_holder"].release()


def check_persons_publish(world, client, events):
    shot = world.extra["shot"]
    accepted = client.get("/api/persons/review/shots", params={"status": "accepted"}).json()["items"]
    assert [s["shot_id"] for s in accepted] == [shot], accepted
    assert any(
        any(s.get("role_id") == world.extra["role"] for s in c.get("selections") or [])
        for c in calls_of(events, "review_shot")
    ), "审核必须指定角色"
    assert ok_results(events, "list_review_shots") and ok_results(events, "review_shot")
    assert ok_results(events, "publish_persons"), "没有发布人物检索"
    asked = {
        a["name"]: a["risk"]
        for a in (e["data"] for e in events if e["type"] == "plugin:mindex/approval-asked")
    }
    assert asked.get("review_shot") == "write" and asked.get("publish_persons") == "danger", asked
    release = Store(world.config.data_dir).active_index()["search_release_id"]
    assert release and release != world.extra["release_before"], "发布后应切换到新的检索组合"


def check_vision(world, client, events):
    user = next(
        e["data"] for e in events if e["type"] == "user/message" and e["data"]["source"]["kind"] == "user"
    )
    assert any(b["type"] == "image" and b.get("ref", "").startswith("attachment://") for b in user["content"])
    frames = ok_results(events, "sample_frames")
    assert frames, "没有抽帧"
    images = frames[-1]["message"].get("images") or []
    assert images and NO_VISION_TOOL_NOTE not in text_of(frames[-1])
    requests = scripted_requests(client)
    if requests is not None:
        assert len(image_blocks(requests[0])) == 1, "首步请求应带上用户附件图片"
        assert len(image_blocks(requests[-1])) == 1 + len(images), "抽帧画面应进入后续请求"
        assert all(NO_VISION_PROMPT not in r.system for r in requests)
    assert "红" in final_text(events), "应说明截图的主色"
    assert "data:image/" not in json.dumps(events, ensure_ascii=False), "日志不应含图片数据"


def check_no_vision(world, client, events):
    frames = ok_results(events, "sample_frames")
    assert frames and frames[-1]["message"].get("images"), "没有抽帧"
    assert NO_VISION_TOOL_NOTE in text_of(frames[-1]), "视觉关闭时应注明图片仅在面板展示"
    requests = scripted_requests(client)
    if requests is not None:
        assert NO_VISION_PROMPT in requests[-1].system, "有图片但视觉关闭时系统提示应说明"
        assert not any(image_blocks(r) for r in requests), "视觉关闭时不应发送图片"
    assert "data:image/" not in json.dumps(events, ensure_ascii=False)


# ------------------------------------------------------------ 能力扩展场景：确定性脚本
def cross_domain_script(world):
    return [
        [call("search_media", text="蓝色 画面")],  # search 领域尚未加载 → TOOL_NOT_LOADED
        [call("load_tools", domains=["search"])],
        [call("search_media", text="蓝色 画面")],
        [call("load_tools", domains=["index", "persons"])],  # 第 4 个领域：淘汰最久未用的 studio
        [call("list_index_versions"), call("list_persons")],
        [say("找到了蓝色画面的片段；索引版本与人物库情况如上。")],
    ]


def attachment_search_script(world):
    return [
        lambda request: [
            call("search_by_media", attachment=attachment_ref(request), granularity="shot", limit=3)
        ],
        [say("最相似的是红色开场镜头。")],
    ]


def similar_script(world):
    def similar(request):
        seed = re.search(r"^#1 .* 片段 (\S+)", last_tool(request), re.M).group(1)
        return [call("find_similar", segment_id=seed, limit=5)]

    return [
        [call("search_media", text="绿色", granularity="shot", limit=3)],
        similar,
        [say("这些是与该绿色镜头画面相似的片段。")],
    ]


def index_rollback_script(world):
    older = world.extra["older"]
    return [
        [call("load_tools", domains=["index"])],
        [call("list_index_versions")],
        [call("check_index", index_id=older)],
        [call("activate_index", index_id=older)],
        [say("已回退到上一个索引版本。")],
    ]


def cancel_build_script(world):
    job = world.extra["slow_job"]
    return [
        [call("run_build", job_id=job)],
        [call("cancel_build", job_id=job)],
        [say("已请求取消，正在执行的节点完成后停止。")],
        [say("构建已取消，已生成的版本会保留。")],
    ]


def cancel_task_script(world):
    def cancel(request):
        task = re.search(r"素材任务 (\w+)", last_tool(request)).group(1)
        return [call("cancel_task", kind="source", task_id=task)]

    return [
        [call("start_source_task", source_id=world.source_id, work="publish", target="shots")],
        cancel,
        [say("已取消该发布任务。")],
        [say("发布任务已取消，活动索引未改变；需要时可用 retry_task 重试。")],
    ]


def persons_publish_script(world):
    selection = {"actor_id": world.extra["actor"], "role_id": world.extra["role"]}
    return [
        [call("list_review_shots", status="candidate")],
        [call("review_shot", source_id=world.source_id, shot_id=world.extra["shot"], selections=[selection])],
        [call("publish_persons")],
        [say("已确认出镜并发布人物检索。")],
    ]


def vision_script(world):
    return [
        [call("load_tools", domains=["search"])],
        [call("sample_frames", source_id=world.source_id, start=0, end=2, count=3)],
        [say("截图主要是红色；开头三帧依次是红色、蓝色和绿色画面。")],
    ]


def no_vision_script(world):
    return [
        [call("load_tools", domains=["search"])],
        [call("sample_frames", source_id=world.source_id, start=0, end=2, count=3)],
        [say("已抽取 3 帧，画面在面板展示。")],
    ]


# ------------------------------------------------------------ 创作扩展场景：文案模板、参考文稿、画幅、
# 画面来源
INTERVIEW = "问：您做这行多久了？\n答：三十年了，手上的活一天都没停过。"


def setup_interview(world):
    """新式任务（只有大纲、尚无文案）；大纲清空，改写模板的文案完全来自参考文稿。"""
    job = studio.create_job(world.config, "讲讲一位老匠人的一天。")["id"]
    state = studio.load_job(world.config, job)
    studio.save(studio.job_path(world.config, job), {**state, "outline": ""})
    world.extra["interview_job"] = job


def interview_attachment(world):
    return [("匠人访谈.txt", INTERVIEW.encode("utf-8"), "text/plain")]


def check_interview(world, client, events):
    data = workspace(client, world.extra["interview_job"])
    p = data["plans"][data["active_plan"]]
    assert p["options"]["template_id"] == "interview", p["options"]
    references = p["options"].get("references") or []
    assert len(references) == 1 and references[0]["chars"] == len(INTERVIEW), references
    assert p.get("script"), "没有生成文案"
    script = data["versions"][p["script"]]
    assert script["status"] == "complete" and script["data"]["text"] == INTERVIEW, script["data"]
    assert [r["id"] for r in script["data"]["references"]] == [references[0]["id"]]
    assert ok_results(events, "add_reference_text") and ok_results(events, "run_node")
    finished = [e["data"] for e in events if e["type"] == "plugin:mindex/build-finished"]
    assert finished and finished[-1]["status"] == "complete", finished
    assert followups(events), "文案生成结束后应有 followup 总结"


def interview_script(world):
    job = world.extra["interview_job"]
    return [
        lambda request: [call("add_reference_text", job_id=job, attachment=attachment_ref(request))],
        [call("update_plan_options", job_id=job, changes={"template_id": "interview"})],
        [call("run_node", job_id=job, stage="script")],
        [say("已登记访谈稿并开始按访谈改写模板生成文案。")],
        [say("文案已按访谈稿改写完成。")],
    ]


def built_job(world, title, ids, texts=("红色开场。", "绿色结尾。")):
    """分镜任务（缺省两镜），完整构建一次成片；返回 (job, plan)。"""

    job = legacy_job(world.config, "".join(texts), options={"burn": False}, title=title)["id"]
    plan = board(world.config, job, list(texts), ids)
    with TestClient(world.app()) as client:
        token = client.get(f"/api/studio/jobs/{job}/plans/{plan}/preflight").json()["token"]
        build = client.post(f"/api/studio/jobs/{job}/plans/{plan}/build", json={"token": token}).json()
        deadline = time.monotonic() + 120
        while client.get(f"/api/studio/jobs/{job}/builds/{build['id']}/logs").json()["status"] == "running":
            assert time.monotonic() < deadline, f"{title}构建未结束"
            time.sleep(0.1)
        assert workspace(client, job)["builds"][-1]["status"] == "complete"
    return job, plan


def setup_built_job(world):
    """供换画幅后预检的已构建任务。"""
    job, plan = built_job(world, "横屏短片", ["shot-c4a5e1aa01", "shot-c4a5e1aa02"])
    world.extra.update(built_job=job, built_plan=plan)


def check_portrait(world, client, events):
    job, original = world.extra["built_job"], world.extra["built_plan"]
    data = workspace(client, job)
    created = [pid for pid in data["plans"] if pid != original]
    assert len(created) == 1, f"应新建一个竖屏方案：{list(data['plans'])}"
    pid = created[0]
    assert data["plans"][pid]["options"].get("canvas") == "9:16", data["plans"][pid]["options"]
    assert data["plans"][original]["options"].get("canvas", "16:9") == "16:9", "原方案不应改画幅"
    nodes = client.get(f"/api/studio/jobs/{job}/plans/{pid}/preflight").json()["nodes"]
    rebuilt = {n["stage"] for n in nodes if n["action"] != "reuse"}
    assert rebuilt == {"render", "compose"}, nodes
    assert all(n["action"] in ("reuse", "rebuild") for n in nodes), nodes
    checked = ok_results(events, "get_preflight")
    assert checked, "没有做构建预检"
    lines = [line for line in text_of(checked[-1]).splitlines() if line.startswith("- ")]
    assert lines and all(line.startswith(("- 渲染", "- 合成")) for line in lines), lines


def portrait_script(world):
    job, plan = world.extra["built_job"], world.extra["built_plan"]

    def options(request):
        pid = re.search(r"\(([^)]+)\) 并设为当前方案", last_tool(request)).group(1)
        world.extra["portrait_plan"] = pid
        return [
            call("update_plan_options", job_id=job, plan_id=pid, changes={"canvas": "9:16", "fit": "crop"})
        ]

    return [
        [call("create_plan", job_id=job, name="竖屏版", copy_from=plan)],
        options,
        lambda request: [call("get_preflight", job_id=job, plan_id=world.extra["portrait_plan"])],
        [say("已新建竖屏方案；预检显示只需重建渲染与成片，召回、配音、选片全部复用。")],
    ]


SOURCE_SHOT = SHOTS[3]


def check_attachment_source(world, client, events):
    data = workspace(client, world.job)
    p = data["plans"][world.plan]
    content = data["versions"][p["shots"][SOURCE_SHOT]["content"]]["data"]
    source = content.get("source") or {}
    assert source.get("kind") == "image" and source.get("motion") == "pan-left", source
    assert re.fullmatch(r"[0-9a-f]{64}", source.get("media_id") or ""), source
    assert data["shot_stages"][SOURCE_SHOT] == ["tts", "render"], data["shot_stages"]
    attached = calls_of(events, "attach_query_media")
    assert attached and attached[-1].get("purpose") == "source", attached
    details = ok_results(events, "get_shot_details")
    assert details, "没有读取分镜详情"
    text = text_of(details[-1])
    assert '"kind":"image"' in text and '"motion":"pan-left"' in text, text
    others = [data["versions"][p["shots"][sid]["content"]]["data"] for sid in SHOTS[:3]]
    assert not any(item.get("source") for item in others), "其他分镜不应改画面来源"


def attachment_source_script(world):
    job, plan = world.job, world.plan

    def edit(request):
        media = re.search(r'"media_id":"([0-9a-f]{64})"', last_tool(request)).group(1)
        return [
            call(
                "edit_shots",
                job_id=job,
                plan_id=plan,
                operations=[
                    {
                        "op": "update",
                        "shot": "#4",
                        "fields": {"source": {"kind": "image", "media_id": media, "motion": "pan-left"}},
                    }
                ],
            )
        ]

    return [
        lambda request: [
            call(
                "attach_query_media",
                job_id=job,
                plan_id=plan,
                shot="#4",
                attachment=attachment_ref(request),
                purpose="source",
            )
        ],
        [call("get_shot_details", job_id=job, plan_id=plan, refs=["#4"])],
        edit,
        [call("get_shot_details", job_id=job, plan_id=plan, refs=["#4"])],
        [say("第 4 镜已改用你的图片，画面向左平移；下次构建只重建该镜渲染与成片。")],
    ]


AIGC_SHOTS = ["shot-a16cc0aa01", "shot-a16cc0aa02"]
AIGC_PROMPT = "极光下的雪山，镜头缓慢推进"


def setup_aigc(world):
    """已构建的两镜任务，并启用视频生成（服务由 World 注入的确定性桩提供）。"""
    job, plan = built_job(world, "极光短片", AIGC_SHOTS)
    generator = world.config.studio.video_generator
    generator.enabled, generator.backend, generator.model = True, "minimax", "scenario-hailuo"
    generator.durations, generator.poll_interval, generator.max_wait = [1.0, 2.0, 3.0], 0.01, 60
    world.extra.update(aigc_job=job, aigc_plan=plan)


def check_aigc(world, client, events):
    job, plan = world.extra["aigc_job"], world.extra["aigc_plan"]
    sid = AIGC_SHOTS[1]
    data = workspace(client, job)
    p = data["plans"][plan]
    source = data["versions"][p["shots"][sid]["content"]]["data"].get("source") or {}
    assert (
        source.get("kind") == "aigc" and source.get("prompt") == AIGC_PROMPT and source.get("seed") == 42
    ), source
    assert data["shot_stages"][sid] == ["tts", "generate", "select", "render"], data["shot_stages"]
    version = data["versions"][p["shots"][sid]["generate"]]
    assert version["status"] == "complete" and "generated" in version["files"], version
    requests = world.video_generator.requests
    assert len(requests) == 1 and requests[0].prompt == AIGC_PROMPT and requests[0].seed == 42, requests
    assert ok_results(events, "edit_shots") and ok_results(events, "run_node")
    finished = [e["data"] for e in events if e["type"] == "plugin:mindex/build-finished"]
    assert finished and finished[-1]["status"] == "complete", finished
    details = ok_results(events, "get_shot_details")
    assert details and "生成画面：版本" in text_of(details[-1]), details
    media = (details[-1].get("meta") or {}).get("media") or []
    assert any(
        m["kind"] == "video" and m["url"].startswith("/api/") and "生成画面" in m["title"] for m in media
    ), media
    first = data["versions"][p["shots"][AIGC_SHOTS[0]]["content"]]["data"]
    assert not first.get("source"), "其他分镜不应改画面来源"


def aigc_script(world):
    job, plan = world.extra["aigc_job"], world.extra["aigc_plan"]
    return [
        [
            call(
                "edit_shots",
                job_id=job,
                plan_id=plan,
                operations=[
                    {
                        "op": "update",
                        "shot": "#2",
                        "fields": {"source": {"kind": "aigc", "prompt": AIGC_PROMPT, "seed": 42}},
                    }
                ],
            )
        ],
        [call("run_node", job_id=job, plan_id=plan, stage="generate", shot="#2")],
        [say("已提交第 2 镜的 AI 生成视频，完成后告诉你。")],
        [call("get_shot_details", job_id=job, plan_id=plan, refs=["#2"])],
        [say("第 2 镜的画面已生成；下次构建会在生成片段内选片并重新渲染成片。")],
    ]


ANIMATION_SHOTS = ["shot-a71a7e0a01", "shot-a71a7e0a02"]
ANIMATION_BRIEF = "柱状图展示 2020–2024 年销量逐年增长"


def setup_animation(world):
    """已构建的两镜任务（动画生成与录制由 World 注入的确定性替身提供）。"""
    job, plan = built_job(world, "动画短片", ANIMATION_SHOTS)
    world.config.studio.animator.model = "scenario-animator"
    world.extra.update(animation_job=job, animation_plan=plan)


def check_animation(world, client, events):
    job, plan = world.extra["animation_job"], world.extra["animation_plan"]
    sid = ANIMATION_SHOTS[1]
    data = workspace(client, job)
    p = data["plans"][plan]
    source = data["versions"][p["shots"][sid]["content"]]["data"].get("source") or {}
    assert source == {"kind": "animation", "brief": ANIMATION_BRIEF}, source
    assert data["shot_stages"][sid] == ["tts", "animate", "render"], data["shot_stages"]
    animated = data["versions"][p["shots"][sid]["animate"]]
    assert animated["status"] == "complete" and animated["data"]["origin"] == "manual", animated
    html = client.get(f"/api/studio/jobs/{job}/versions/{animated['id']}/animation.html").text
    assert "#ff8800" in html and SCENARIO_BAR not in html, html
    assert len(world.animator.calls) == 1 and world.animator.calls[0]["brief"] == ANIMATION_BRIEF, (
        world.animator.calls
    )
    render = data["versions"][p["shots"][sid]["render"]]
    assert render["status"] == "complete" and render["inputs"]["animate"] == animated["id"], render["inputs"]
    for name in (
        "edit_shots",
        "run_node",
        "get_animation_source",
        "edit_animation_source",
        "get_shot_details",
    ):
        assert ok_results(events, name), f"没有成功调用 {name}"
    stages = [call.get("stage") for call in calls_of(events, "run_node")]
    assert "animate" in stages and "render" in stages, stages
    details = ok_results(events, "get_shot_details")
    assert "动画源码：版本" in text_of(details[-1]) and "人工修改" in text_of(details[-1])
    media = (details[-1].get("meta") or {}).get("media") or []
    assert any(
        m["kind"] == "video" and m["url"].startswith("/api/") and "动画录制" in m["title"] for m in media
    ), media
    first = data["versions"][p["shots"][ANIMATION_SHOTS[0]]["content"]]["data"]
    assert not first.get("source"), "其他分镜不应改画面来源"


def animation_script(world):
    job, plan = world.extra["animation_job"], world.extra["animation_plan"]
    return [
        [
            call(
                "edit_shots",
                job_id=job,
                plan_id=plan,
                operations=[
                    {
                        "op": "update",
                        "shot": "#2",
                        "fields": {"source": {"kind": "animation", "brief": ANIMATION_BRIEF}},
                    }
                ],
            )
        ],
        [call("run_node", job_id=job, plan_id=plan, stage="animate", shot="#2")],
        [say("已提交第 2 镜的动画生成，完成后我来改颜色。")],
        [call("load_tools", domains=["studio_detail"])],
        [call("get_animation_source", job_id=job, plan_id=plan, shot="#2")],
        [
            call(
                "edit_animation_source",
                job_id=job,
                plan_id=plan,
                shot="#2",
                replacements=[{"find": SCENARIO_BAR, "replace": "#ff8800"}],
            )
        ],
        [call("run_node", job_id=job, plan_id=plan, stage="render", shot="#2")],
        [say("柱子已改成橙色，正在重新录制第 2 镜。")],
        [call("get_shot_details", job_id=job, plan_id=plan, refs=["#2"])],
        [say("第 2 镜的动画已按橙色柱子重新录制，可以在面板里播放。")],
    ]


AI_IMAGE_SHOTS = ["shot-a1a9e0aa01", "shot-a1a9e0aa02"]
AI_IMAGE_PROMPT = "清晨薄雾中的竹林小径，柔和逆光"


def setup_ai_image(world):
    """已构建的两镜任务，并启用图片生成（服务由 World 注入的确定性桩提供）。"""
    job, plan = built_job(world, "竹林短片", AI_IMAGE_SHOTS)
    world.config.studio.image_generator.enabled = True
    world.extra.update(image_job=job, image_plan=plan)


def check_ai_image(world, client, events):
    job, plan = world.extra["image_job"], world.extra["image_plan"]
    sid = AI_IMAGE_SHOTS[1]
    data = workspace(client, job)
    p = data["plans"][plan]
    source = data["versions"][p["shots"][sid]["content"]]["data"].get("source") or {}
    assert source.get("kind") == "aigc_image" and source.get("prompt") == AI_IMAGE_PROMPT, source
    assert source.get("motion") == "pan-right", source
    assert data["shot_stages"][sid] == ["tts", "draw", "render"], data["shot_stages"]
    drawn = data["versions"][p["shots"][sid]["draw"]]
    assert drawn["status"] == "complete" and "image" in drawn["files"], drawn
    assert [r.prompt for r in world.image_generator.requests] == [AI_IMAGE_PROMPT]
    assert ok_results(events, "edit_shots") and ok_results(events, "run_node")
    details = ok_results(events, "get_shot_details")
    assert details and "生成图片：版本" in text_of(details[-1]), details


def ai_image_script(world):
    job, plan = world.extra["image_job"], world.extra["image_plan"]
    return [
        [
            call(
                "edit_shots",
                job_id=job,
                plan_id=plan,
                operations=[
                    {
                        "op": "update",
                        "shot": "#2",
                        "fields": {
                            "source": {"kind": "aigc_image", "prompt": AI_IMAGE_PROMPT, "motion": "pan-right"}
                        },
                    }
                ],
            )
        ],
        [call("run_node", job_id=job, plan_id=plan, stage="draw", shot="#2")],
        [say("已提交第 2 镜的 AI 生成图片，完成后告诉你。")],
        [call("get_shot_details", job_id=job, plan_id=plan, refs=["#2"])],
        [say("第 2 镜的图片已生成，渲染时会向右平移。")],
    ]


BGM_SHOTS = ["shot-b6a0c0aa01", "shot-b6a0c0aa02", "shot-b6a0c0aa03"]
BGM_PROMPT = "梦幻流行，女声，适合极光旅行短片"


def setup_bgm(world):
    job, plan = built_job(world, "配乐短片", BGM_SHOTS, ("红色开场。", "蓝色中段。", "绿色结尾。"))
    world.config.studio.music_generator.enabled = True
    world.extra.update(bgm_job=job, bgm_plan=plan)


def check_bgm(world, client, events):
    job, plan = world.extra["bgm_job"], world.extra["bgm_plan"]
    data = workspace(client, job)
    p = data["plans"][plan]
    cue = data["versions"][p["shots"][BGM_SHOTS[1]]["content"]]["data"].get("music") or {}
    assert (
        cue.get("kind") == "generate" and cue.get("prompt") == BGM_PROMPT and cue.get("instrumental") is False
    ), cue
    assert "[Chorus]" in (cue.get("lyrics") or ""), cue
    first = data["versions"][p["shots"][BGM_SHOTS[0]]["content"]]["data"]
    assert not first.get("music"), "第 1 镜不应有背景音乐"
    assert data["shot_music"] == {
        BGM_SHOTS[1]: {"from": BGM_SHOTS[1], "start": True},
        BGM_SHOTS[2]: {"from": BGM_SHOTS[1], "start": False},
    }, data["shot_music"]
    music = data["versions"][p["shots"][BGM_SHOTS[1]]["music"]]
    assert music["status"] == "complete" and "music" in music["files"], music
    assert world.music_generator.lyric_calls and len(world.music_generator.requests) == 1
    assert (
        ok_results(events, "generate_lyrics")
        and ok_results(events, "edit_shots")
        and ok_results(events, "run_node")
    )


def bgm_script(world):
    job, plan = world.extra["bgm_job"], world.extra["bgm_plan"]

    def edit(request):
        lyrics = json.loads(last_tool(request))["lyrics"] if last_tool(request).startswith("{") else None
        lyrics = lyrics or re.search(
            r"(\[Verse\][\s\S]*啦啦啦)", last_tool(request).replace("\\n", "\n")
        ).group(1)
        return [
            call(
                "edit_shots",
                job_id=job,
                plan_id=plan,
                operations=[
                    {
                        "op": "update",
                        "shot": "#2",
                        "fields": {
                            "music": {
                                "kind": "generate",
                                "prompt": BGM_PROMPT,
                                "instrumental": False,
                                "lyrics": lyrics,
                            }
                        },
                    }
                ],
            )
        ]

    return [
        [call("load_tools", domains=["studio_detail"])],
        [call("generate_lyrics", job_id=job, prompt=BGM_PROMPT)],
        edit,
        [call("run_node", job_id=job, plan_id=plan, stage="music", shot="#2")],
        [say("已从第 2 镜起设置背景音乐并提交生成，之后的分镜会顺延这段音乐。")],
        [say("背景音乐已生成；下次构建成片时会混在配音下面。")],
    ]


SCENARIOS = [
    Scenario(
        "custom_model",
        "我想使用自定义模型配置：拆分镜改用我自建的模型服务",
        check_custom_model,
        custom_model_script,
        form={
            "url": "http://127.0.0.1:9/v1",
            "endpoint": "http://127.0.0.1:9/v1",
            "model": "qwen3-self",
            "key": SECRET,
            "secret": SECRET,
        },
    ),
    Scenario(
        "split_granularity",
        "我要更改文案拆分镜的粒度，拆粗一点",
        check_gap,
        lambda w: [
            [
                say("目前拆分镜粒度没有可调参数；可以用 edit_shots 手动合并相邻分镜。"),
                call(
                    "report_capability_gap",
                    request="调整拆分镜粒度",
                    reason="粒度写死在提示词里",
                    suggestion="手动合并分镜",
                ),
            ],
            [say("已记录。")],
        ],
    ),
    Scenario(
        "merge_rebuild",
        "合并 925281d5bb 和 925281d6 两个分镜，并重新生成该分镜的后续流程",
        check_merge_build,
        merge_script,
        page=lambda w: {"page": "studio", "job_id": w.job, "plan_id": w.plan},
    ),
    Scenario(
        "switch_plan",
        "把当前方案切换到「备选方案」",
        check_switch_plan,
        lambda w: [
            [call("get_workspace_summary", job_id=w.job)],
            [call("activate_plan", job_id=w.job, plan_id=w.other_plan)],
            [say("已切换。")],
        ],
        page=lambda w: {"page": "studio", "job_id": w.job},
    ),
    Scenario(
        "change_voice",
        "把当前方案的配音音色改成 Ethan",
        check_voice,
        lambda w: [
            [call("update_plan_options", job_id=w.job, changes={"voice": "Ethan"})],
            [say("已修改。")],
        ],
        page=lambda w: {"page": "studio", "job_id": w.job, "plan_id": w.plan},
    ),
    Scenario(
        "diagnose_build",
        "「故障任务」最近一次构建为什么失败？",
        check_diagnose,
        lambda w: [
            [call("load_tools", domains=["studio"])],
            [call("list_jobs")],
            [call("get_build_status", job_id=w.broken_job)],
            lambda request: [say("失败原因：" + re.search(r"(TTS[^\n]*)", last_tool(request)).group(1))],
        ],
    ),
    Scenario(
        "search",
        "帮我找一段绿色画面的素材",
        check_search,
        lambda w: [[call("search_media", text="绿色 画面")], [say("找到了这些片段。")]],
        page=lambda w: {"page": "search"},
    ),
    Scenario(
        "ambiguous_id",
        "删除分镜 925281d",
        check_ambiguous,
        ambiguous_script,
        choose="#2",
        page=lambda w: {"page": "studio", "job_id": w.job},
    ),
    Scenario(
        "rejected",
        "删除「颜色短片」这个创作任务",
        check_rejected,
        lambda w: [
            [call("load_tools", domains=["studio"])],
            [call("delete_job", job_id=w.job)],
            [say("好的，已取消删除。")],
        ],
        approve="reject",
    ),
    Scenario(
        "secret",
        "我要换一个自建的拆分镜模型，地址和密钥我来提供",
        check_secret,
        custom_model_script,
        form={
            "url": "http://127.0.0.1:9/v1",
            "endpoint": "http://127.0.0.1:9/v1",
            "model": "qwen3-self",
            "key": SECRET,
            "secret": SECRET,
        },
    ),
    # ---------------- 能力扩展：动态加载、附件、找相似、索引、取消、人物、视觉
    Scenario(
        "cross_domain_load",
        "帮我找一段蓝色画面的素材；另外看看现在有哪些内容索引版本，人物库里登记了哪些人物。",
        check_cross_domain,
        cross_domain_script,
        page=lambda w: {"page": "studio", "job_id": w.job},
        note="创作页出发跨三个领域：未加载即调用得到 TOOL_NOT_LOADED 后自行 load_tools；已加载领域不超过 3 "
        "个",
    ),
    Scenario(
        "attachment_search",
        "用我上传的这张截图，在素材库里找画面相似的镜头",
        check_attachment_search,
        attachment_search_script,
        page=lambda w: {"page": "search"},
        attachments=red_attachment,
        note="面板图片附件 → search_by_media(attachment=…)；红色截图应命中红色开场",
    ),
    Scenario(
        "similar_search",
        "找一个绿色画面的镜头，再找几个和它画面相似的片段",
        check_similar,
        similar_script,
        page=lambda w: {"page": "search"},
        note="search_media 的结果片段作为种子调用 find_similar",
    ),
    Scenario(
        "index_rollback",
        "最新的内容索引有问题，帮我回退到上一个索引版本；回退前先检查一下那个版本是否完好",
        check_index_rollback,
        index_rollback_script,
        page=lambda w: {"page": "settings"},
        setup=setup_index_versions,
        note="list_index_versions → check_index → activate_index（danger 确认卡）",
    ),
    Scenario(
        "cancel_build",
        "先开始构建「慢速任务」的成片，开始后马上帮我取消这次构建（我只是确认取消流程）",
        check_cancel_build,
        cancel_build_script,
        page=lambda w: {"page": "studio", "job_id": w.extra["slow_job"], "plan_id": w.extra["slow_plan"]},
        setup=setup_slow_job,
        note="run_build 后 cancel_build（write）；构建以 cancelled 结束并收到 followup",
    ),
    Scenario(
        "cancel_source_task",
        "给这个素材发起一次分镜结果的检索发布任务，提交后立刻帮我取消它（我只是确认取消流程）",
        check_cancel_task,
        cancel_task_script,
        page=admin_page,
        setup=setup_gpu_busy,
        note="start_source_task（danger，GPU 被占用而排队）→ cancel_task（write）→ task-finished followup",
    ),
    Scenario(
        "persons_review_publish",
        "出镜审核里有个待确认的镜头，画面里是演员甲饰演的叶淑柔；帮我确认，然后发布人物检索",
        check_persons_publish,
        persons_publish_script,
        page=lambda w: {"page": "persons"},
        setup=setup_persons,
        note="list_review_shots → review_shot（write）→ publish_persons（danger）",
    ),
    Scenario(
        "vision",
        "我上传了一张截图，看看它主要是什么颜色；再从这个素材开头 2 秒抽 3 帧给我看",
        check_vision,
        vision_script,
        page=admin_page,
        setup=setup_vision,
        attachments=red_attachment,
        needs=("vision",),
        note="视觉开启：用户图片附件与 sample_frames 画面都以图片进入模型请求（需支持图片输入的模型）",
    ),
    Scenario(
        "no_vision",
        "从这个素材开头 2 秒抽 3 帧画面给我看看",
        check_no_vision,
        no_vision_script,
        page=admin_page,
        note="视觉关闭：不发送图片，工具结果注明仅在面板展示，系统提示说明图片未发送",
    ),
    # ---------------- 创作扩展：访谈改写、竖屏方案、附件作画面、AIGC 分镜、AI 动画分镜
    Scenario(
        "interview_rewrite",
        "把我上传的访谈稿登记为参考文稿，用访谈改写模板生成这个任务的文案",
        check_interview,
        interview_script,
        page=lambda w: {"page": "studio", "job_id": w.extra["interview_job"]},
        setup=setup_interview,
        attachments=interview_attachment,
        note="add_reference_text(附件) → update_plan_options(template_id=interview) → run_node(script)；"
        "文案由参考文稿改写",
    ),
    Scenario(
        "portrait_plan",
        "给「横屏短片」新建一个竖屏 9:16 的方案（铺满裁切），先看看构建时哪些要重做",
        check_portrait,
        portrait_script,
        page=lambda w: {"page": "studio", "job_id": w.extra["built_job"]},
        setup=setup_built_job,
        note="create_plan(复制) → update_plan_options(canvas=9:16) → get_preflight：只重建渲染与成片",
    ),
    Scenario(
        "attachment_source",
        "用我上传的这张图片作为第 4 个分镜的画面，并让画面向左平移",
        check_attachment_source,
        attachment_source_script,
        page=lambda w: {"page": "studio", "job_id": w.job, "plan_id": w.plan},
        attachments=red_attachment,
        note="attach_query_media(purpose=source) → edit_shots(source.motion=pan-left) → get_shot_details",
    ),
    Scenario(
        "aigc_shot",
        "素材库里没有极光雪山的画面，把「极光短片」第 2 个分镜改成 AI 生成视频："
        f"提示词「{AIGC_PROMPT}」，种子 42，然后单独生成这一镜的画面给我看",
        check_aigc,
        aigc_script,
        page=lambda w: {"page": "studio", "job_id": w.extra["aigc_job"], "plan_id": w.extra["aigc_plan"]},
        setup=setup_aigc,
        note="edit_shots(source=aigc) → run_node(generate，danger，提示费用) → "
        "get_shot_details（生成画面媒体）；"
        "视频生成服务为确定性桩",
    ),
    Scenario(
        "ai_image_shot",
        f"把「竹林短片」第 2 个分镜改成 AI 生成图片：提示词「{AI_IMAGE_PROMPT}」，"
        "画面向右平移，然后单独生成这一镜的图片",
        check_ai_image,
        ai_image_script,
        page=lambda w: {"page": "studio", "job_id": w.extra["image_job"], "plan_id": w.extra["image_plan"]},
        setup=setup_ai_image,
        note="edit_shots(source=aigc_image) → run_node(draw，提示费用) → "
        "get_shot_details；图片生成服务为确定性桩",
    ),
    Scenario(
        "background_music",
        f"给「配乐短片」从第 2 个分镜开始加一段带人声的背景音乐：{BGM_PROMPT}，"
        "先帮我写歌词，然后生成这段音乐",
        check_bgm,
        bgm_script,
        page=lambda w: {"page": "studio", "job_id": w.extra["bgm_job"], "plan_id": w.extra["bgm_plan"]},
        setup=setup_bgm,
        note="load_tools(studio_detail) → generate_lyrics → edit_shots(music) → run_node(music)；"
        "音乐生成服务为确定性桩，第 3 镜顺延",
    ),
    Scenario(
        "animation_shot",
        f"把「动画短片」第 2 个分镜改成 AI 动画，画面说明「{ANIMATION_BRIEF}」；生成动画后"
        "把柱子颜色改成橙色 #ff8800，重新录制这一镜给我看",
        check_animation,
        animation_script,
        page=lambda w: {
            "page": "studio",
            "job_id": w.extra["animation_job"],
            "plan_id": w.extra["animation_plan"],
        },
        setup=setup_animation,
        note="edit_shots(source=animation) → run_node(animate) → get_animation_source → "
        "edit_animation_source(查找替换，预览校验) → run_node(render) → get_shot_details（录制媒体）；"
        "动画生成与录制为确定性替身",
    ),
]


def answer_form(scenario: Scenario, question: dict) -> dict:
    answer = {"id": question["id"], "selected": []}
    options = [o["label"] for o in question.get("options") or []]
    if options:
        preferred = [o for o in options if scenario.choose and scenario.choose in o]
        answer["selected"] = [(preferred or options)[0]]
    if question.get("fields"):
        values = {}
        for item in question["fields"]:
            words = f"{item['id']} {item['label']}".lower()
            hit = next((v for k, v in scenario.form.items() if k in words), None)
            values[item["id"]] = hit if hit is not None else (SECRET if item.get("type") == "secret" else "")
        answer["values"] = values
    if not options and not question.get("fields"):
        answer["custom"] = scenario.form.get("custom", "按你的建议")
    return answer


def prepare(scenario: Scenario, config: Config, video: Path) -> World:
    """建公共数据并执行场景专属准备（必须在创建应用之前完成）。"""
    world = build_world(config, video)
    if scenario.setup:
        scenario.setup(world)
    return world


WATCH_PAIRS = (
    ("plugin:mindex/build-watch", "plugin:mindex/build-finished"),
    ("plugin:mindex/task-watch", "plugin:mindex/task-finished"),
)


def drive(client, scenario: Scenario, world: World, *, timeout: float = 180) -> list[dict]:
    """发送请求并自动答复确认与提问，直到会话空闲且没有在跟踪的构建或后台任务。"""
    sid = client.post("/api/agent/sessions", json={}).json()["id"]
    context = scenario.page(world) if scenario.page else None
    refs = []
    for name, data, content_type in scenario.attachments(world) if scenario.attachments else []:
        response = client.post(
            "/api/agent/attachments", files={"file": (name, data, content_type)}, data={"session_id": sid}
        )
        assert response.status_code == 201, response.text
        refs.append(response.json()["id"])
    body = {"content": scenario.prompt, "context": context, **({"attachments": refs} if refs else {})}
    response = client.post(f"/api/agent/sessions/{sid}/messages", json=body)
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + timeout
    answered = set()
    quiet_since = None
    while time.monotonic() < deadline:
        session = client.get(f"/api/agent/sessions/{sid}").json()
        for item in session["pending"]:
            if item["requestId"] in answered:
                continue
            answered.add(item["requestId"])
            if item["kind"] == "approval":
                outcome = "rejected" if scenario.approve == "reject" else "allowed-once"
                client.post(
                    f"/api/agent/sessions/{sid}/approvals/{item['requestId']}", json={"outcome": outcome}
                )
            else:
                client.post(
                    f"/api/agent/sessions/{sid}/questions/{item['requestId']}",
                    json={"answers": [answer_form(scenario, q) for q in item["questions"]]},
                )
        events = client.get(f"/api/agent/sessions/{sid}/log").json()["items"]
        types = [e["type"] for e in events]
        watching = any(types.count(watch) > types.count(finish) for watch, finish in WATCH_PAIRS)
        idle = session["status"] == "idle" and types.count("turn/start") == types.count("turn/end") and types
        if idle and not watching:
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since > 0.3:
                return events
        else:
            quiet_since = None
        time.sleep(0.05)
    raise AssertionError(f"场景 {scenario.name} 超时")
