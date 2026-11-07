"""AI 动画来源：animate 阶段（生成、签名、预检阻塞）、人工保存版本（钉住、只重建该镜）、录制渲染、预览响应头、
五种来源混合合成，以及「动画生成」设置角色。

动画生成模型一律用确定性的 FakeAnimator，录制用 fake_capture（ffmpeg 按源码摘要生成纯色画面、帧数精确），
绝不调用真实 LLM，也不需要 Chromium。真实沙箱录制见 test_animation_capture.py。
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from api import create_app
from conftest import FixtureSpeaker
from shared.media import command
from studio import studio
from studio import workspace as ws
from test_studio_aigc import FakeVideoGenerator, aigc_shot
from test_studio_sources import frame_count, make_clip, make_image, setup, store_input

slow = pytest.mark.slow
SECRET = "sk-animator-secret-1234567890"


def page(label: str, color: str = "#204080") -> str:
    return (
        f"<!DOCTYPE html><html><head><style>html,body{{margin:0;overflow:hidden;background:{color}}}</style>"
        f"</head><body><div id=t>{label}</div><script>requestAnimationFrame(function f(t){{"
        "document.getElementById('t').style.opacity=Math.min(1,t/500);requestAnimationFrame(f)})</script>"
        "</body></html>"
    )


class FakeAnimator:
    """Tests only: 按画面说明返回一份确定性的合规 HTML；记录每次调用。"""

    def __init__(self, html=None):
        self.calls, self.html = [], html

    def animate(self, brief, *, seconds, width, height, fps, previous=None, feedback=None):
        self.calls.append({"brief": brief, "seconds": seconds, "width": width, "height": height, "fps": fps})
        return self.html or page(f"{brief}#{len(self.calls)}")

    def ping(self):
        return {"model": "fake", "reply": "ok"}


class FakeCapture:
    """Tests only: 代替 Chromium 录制——按源码摘要取颜色，用 ffmpeg 生成恰好 frames 帧。"""

    def __init__(self):
        self.calls = []

    def __call__(
        self, html, *, seconds, width, height, fps, target, max_seconds, frame_timeout=5, frames=None
    ):
        self.calls.append({"html": html, "frames": frames, "width": width, "height": height, "fps": fps})
        color = "0x" + hashlib.sha256(html.encode()).hexdigest()[:6]
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
                f"color=c={color}:s={width}x{height}:r={fps}",
                "-frames:v",
                str(frames),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(target),
            ],
            timeout=60,
        )
        return {"frames": frames, "fps": str(fps), "blocked_requests": [], "wall_seconds": 0.01}


@pytest.fixture
def anim(config):
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.preset = "ultrafast"
    config.studio.render.fps = 25
    config.studio.animator.model = "fake-animator"
    config.studio.animator.api_key = SECRET
    return config


def shot(sid, text, **extra):
    return studio.Shot(id=sid, text=text, query="图表", estimated_seconds=1, **extra)


def anim_shot(sid="a", text="增长曲线开场。", **source):
    return shot(sid, text, visual="折线图逐步上升", source={"kind": "animation", **source})


def run(config, job_id, w, p, *, animator, capture, force=False, embedder=None, generator=None):
    checked = ws.preflight(config, job_id, w, p, force=force)
    bid = str(len(w["builds"]))
    w["builds"].append(
        {
            "id": bid,
            "plan_id": p["id"],
            "status": "running",
            "force": force,
            "nodes": [{**n, "status": "pending"} for n in checked["nodes"]],
        }
    )
    ws.persist(config, job_id, w)
    ws.run_build(
        config,
        job_id,
        bid,
        speaker=FixtureSpeaker(config),
        embedder=embedder,
        video_generator=generator,
        animator=animator,
        animation_capture=capture,
    )
    fresh = ws.load(config, job_id)
    return fresh, ws.plan(fresh, p["id"])


def nodes_of(w):
    return {n["key"]: n for n in w["builds"][-1]["nodes"]}


# ---------------------------------------------------------------- 模型、签名与预检（快速）


def test_animation_source_model_and_stage_sequence():
    value = anim_shot(brief="  ").model_dump()["source"]
    assert value == {"kind": "animation", "brief": None}
    assert anim_shot(brief=" 柱状图 ").source.brief == "柱状图"
    assert ws.SHOT_SOURCES["animation"] == ("tts", "animate", "render")
    with pytest.raises(ValueError):
        anim_shot(brief="x" * 2001)
    with pytest.raises(ValueError):
        shot("a", "旁白", source={"kind": "animation", "media_id": "a" * 64})


def test_preflight_and_dependencies(anim):
    job_id, w, p = setup(anim, [anim_shot()])
    nodes = {n["key"]: n for n in ws.preflight(anim, job_id, w, p)["nodes"]}
    assert set(nodes) == {"tts:a", "animate:a", "render:a", "compose"}
    assert nodes["animate:a"]["action"] == "rebuild"
    assert ws.node_dependencies(w, p, "animate", "a") == ["tts:a"]
    assert ws.node_dependencies(w, p, "render", "a") == ["animate:a", "tts:a"]
    anim.studio.animator.enabled = False
    nodes = {n["key"]: n for n in ws.preflight(anim, job_id, w, p)["nodes"]}
    assert nodes["animate:a"]["action"] == "blocked"
    assert nodes["animate:a"]["reason"] == "未配置动画生成服务，请在设置页激活动画生成档案"
    assert nodes["render:a"]["action"] == nodes["compose"]["action"] == "blocked"
    assert nodes["tts:a"]["action"] == "rebuild"


def test_signature_covers_brief_model_and_canvas_but_not_secrets(anim):
    job_id, w, p = setup(anim, [anim_shot()])
    base = ws.specification(anim, job_id, w, p, "animate", "a")
    assert base["inputs"] == {"content": p["shots"]["a"]["content"], "tts": None}
    assert base["parameters"] == {
        "animator": {"backend": "openai", "model": "fake-animator", "max_html_kb": 200},
        "prompt_version": 1,
    }
    assert SECRET not in json.dumps(base)
    signatures = {base["signature"]}
    ws.set_board(anim, job_id, w, p, [anim_shot(brief="柱状图")])
    signatures.add(ws.specification(anim, job_id, w, p, "animate", "a")["signature"])
    ws.set_board(anim, job_id, w, p, [anim_shot()])
    anim.studio.animator.model = "other"
    signatures.add(ws.specification(anim, job_id, w, p, "animate", "a")["signature"])
    anim.studio.animator.model = "fake-animator"
    p["options"]["canvas"] = "9:16"
    signatures.add(ws.specification(anim, job_id, w, p, "animate", "a")["signature"])
    p["options"]["canvas"] = "16:9"
    assert len(signatures) == 4
    anim.studio.animator.timeout = 5
    anim.studio.animator.api_key = "sk-other-000000000"
    assert ws.specification(anim, job_id, w, p, "animate", "a")["signature"] == base["signature"]


def test_manual_save_validates_and_requires_animation_source(anim):
    job_id, w, p = setup(anim, [anim_shot(), shot("b", "旁白。")])
    with pytest.raises(studio.StudioError, match="不是 AI 动画"):
        ws.save_animation(anim, job_id, w, p, "b", page("x"))
    with pytest.raises(studio.StudioError, match="未通过安全检查") as caught:
        ws.save_animation(
            anim, job_id, w, p, "a", page("x").replace("</body>", "<script>fetch('/x')</script></body>")
        )
    assert "fetch" in str(caught.value)
    vid = ws.save_animation(anim, job_id, w, p, "a", page("手写"))
    item = ws.version(w, vid)
    assert item["data"]["origin"] == "manual" and p["shots"]["a"]["animate"] == vid
    assert ws.file_path(anim, job_id, item, "animation").read_text(encoding="utf-8") == page("手写")
    # 人工版本不会按签名被误选，只在选用期间经「钉住」复用。
    spec = ws.specification(anim, job_id, w, p, "animate", "a")
    assert item["signature"] != spec["signature"]
    assert ws.match(anim, job_id, w, p, "animate", "a") == vid
    p["shots"]["a"]["animate"] = None
    assert ws.match(anim, job_id, w, p, "animate", "a") is None


def test_api_rejects_invalid_source_with_chinese_422_and_dry_run(anim):
    client = TestClient(
        create_app(
            anim, speaker=FixtureSpeaker(anim), animator=FakeAnimator(), animation_capture=FakeCapture()
        )
    )
    job_id, _, p = setup(anim, [anim_shot()])
    revision = client.get(f"/api/studio/jobs/{job_id}/workspace").json()["revision"]
    url = f"/api/studio/jobs/{job_id}/plans/{p['id']}/shots/a/animation"
    bad = '<html><body><img src="https://example.com/x.png"><script>new WebSocket("ws://x")</script></body></html>'
    response = client.patch(url, json={"revision": revision, "html": bad})
    assert response.status_code == 422
    assert "外部地址" in response.json()["detail"] and "WebSocket" in response.json()["detail"]
    checked = client.patch(url, json={"revision": revision, "html": bad, "dry_run": True}).json()
    assert checked["valid"] is False and len(checked["errors"]) == 2 and checked["max_kb"] == 200
    assert client.patch(url, json={"revision": revision, "html": page("ok"), "dry_run": True}).json()["valid"]
    assert client.patch(url, json={"revision": "stale", "html": page("ok")}).status_code == 422
    assert (
        client.patch(
            f"/api/studio/jobs/{job_id}/plans/{p['id']}/shots/zz/animation",
            json={"revision": revision, "html": page("ok")},
        ).status_code
        == 404
    )


# ---------------------------------------------------------------- 构建（ffmpeg）


@slow
def test_build_without_index_manual_edit_rebuilds_only_that_shot(anim, tmp_path):
    job_id, w, p = setup(anim, [anim_shot()])
    image_id = store_input(anim, job_id, make_image(tmp_path / "still.png"))
    ws.set_board(
        anim,
        job_id,
        w,
        p,
        [anim_shot(), shot("b", "静态结尾。", source={"kind": "image", "media_id": image_id})],
    )
    ws.persist(anim, job_id, w)
    animator, capture = FakeAnimator(), FakeCapture()
    w, p = run(anim, job_id, w, p, animator=animator, capture=capture)
    build = w["builds"][-1]
    assert build["status"] == "complete", build
    assert [n["key"] for n in build["nodes"]] == [
        "tts:a",
        "tts:b",
        "animate:a",
        "render:a",
        "render:b",
        "compose",
    ]
    [call] = animator.calls
    assert call["brief"] == "折线图逐步上升" and (call["width"], call["height"], call["fps"]) == (
        160,
        120,
        25,
    )
    generated = ws.version(w, p["shots"]["a"]["animate"])
    assert generated["data"]["origin"] == "generated" and set(generated["files"]) == {"animation"}
    render = ws.version(w, p["shots"]["a"]["render"])
    assert render["inputs"]["animate"] == generated["id"]
    assert capture.calls[0]["frames"] == render["data"]["frames"]
    assert {"video", "animation", "preview", "audio"} <= set(render["files"])
    assert frame_count(ws.file_path(anim, job_id, render, "video")) == (render["data"]["frames"], 160, 120)
    final = ws.version(w, p["final"])
    assert frame_count(ws.file_path(anim, job_id, final, "mp4"))[0] == final["data"]["total_frames"]
    detail = ws.detail(anim, job_id, w)
    assert (
        detail["shot_stages"]["a"] == ["tts", "animate", "render"] and detail["conflicts"]["animate:a"] == []
    )
    assert SECRET not in "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in studio.job_dir(anim, job_id).rglob("*.json")
    )
    first_b = p["shots"]["b"]["render"]

    # 人工修改：只重建该镜的渲染与成片；配音、另一镜渲染复用；不调用动画生成模型。
    manual = ws.save_animation(anim, job_id, w, p, "a", page("人工版", "#aa2200"))
    ws.persist(anim, job_id, w)
    nodes = {n["key"]: n for n in ws.preflight(anim, job_id, w, p)["nodes"]}
    assert nodes["animate:a"]["action"] == "reuse" and nodes["animate:a"]["version"] == manual
    assert "人工修改" in nodes["animate:a"]["reason"]
    assert nodes["render:a"]["action"] == "rebuild" and nodes["render:b"]["action"] == "reuse"
    w, p = run(anim, job_id, w, p, animator=animator, capture=capture)
    status = {key: n["status"] for key, n in nodes_of(w).items()}
    assert status == {
        "tts:a": "reused",
        "tts:b": "reused",
        "animate:a": "reused",
        "render:a": "complete",
        "render:b": "reused",
        "compose": "complete",
    }
    assert (
        len(animator.calls) == 1
        and p["shots"]["a"]["animate"] == manual
        and p["shots"]["b"]["render"] == first_b
    )
    assert capture.calls[-1]["html"] == page("人工版", "#aa2200")

    # 改画面说明后普通构建仍保留人工源码（不被重新生成覆盖）。
    ws.set_board(
        anim,
        job_id,
        w,
        p,
        [
            anim_shot(brief="柱状图对比"),
            shot("b", "静态结尾。", source={"kind": "image", "media_id": image_id}),
        ],
    )
    ws.persist(anim, job_id, w)
    w, p = run(anim, job_id, w, p, animator=animator, capture=capture)
    assert nodes_of(w)["animate:a"]["status"] == "reused" and p["shots"]["a"]["animate"] == manual
    assert len(animator.calls) == 1 and ws.detail(anim, job_id, w)["conflicts"]["animate:a"] == []
    # 强制重建才重新生成。
    w, p = run(anim, job_id, w, p, animator=animator, capture=capture, force=True)
    assert len(animator.calls) == 2 and animator.calls[-1]["brief"] == "柱状图对比"
    assert ws.version(w, p["shots"]["a"]["animate"])["data"]["origin"] == "generated"


@slow
def test_api_preview_headers_manual_patch_and_injection(anim):
    animator, capture = FakeAnimator(), FakeCapture()
    client = TestClient(
        create_app(anim, speaker=FixtureSpeaker(anim), animator=animator, animation_capture=capture)
    )
    job_id, _, p = setup(anim, [anim_shot()])
    base = f"/api/studio/jobs/{job_id}/plans/{p['id']}"
    checked = client.get(f"{base}/preflight").json()
    assert client.post(f"{base}/build", json={"token": checked["token"]}).status_code == 202
    data = wait(client, job_id)
    assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
    vid = data["plans"][p["id"]]["shots"]["a"]["animate"]
    response = client.get(f"/api/studio/jobs/{job_id}/versions/{vid}/animation.html")
    assert response.status_code == 200 and response.text == page("折线图逐步上升#1")
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert csp.startswith("default-src 'none'") and "script-src 'unsafe-inline'" in csp
    assert "connect-src" not in csp and "img-src data: blob:" in csp
    assert response.headers["x-content-type-options"] == "nosniff" and "sandbox allow-scripts" in csp
    # 同一份源码经产物下载接口直接打开时同样带沙箱 CSP（不会在本站源里执行脚本）。
    path = data["versions"][vid]["files"]["animation"]["path"]
    artifact = client.get(f"/api/studio/jobs/{job_id}/artifacts/{path}")
    assert (
        artifact.status_code == 200 and "sandbox allow-scripts" in artifact.headers["content-security-policy"]
    )
    video = data["versions"][data["plans"][p["id"]]["shots"]["a"]["render"]]["files"]["video"]["path"]
    assert "content-security-policy" not in client.get(f"/api/studio/jobs/{job_id}/artifacts/{video}").headers
    render_vid = data["plans"][p["id"]]["shots"]["a"]["render"]
    assert client.get(f"/api/studio/jobs/{job_id}/versions/{render_vid}/animation.html").status_code == 422

    saved = client.patch(f"{base}/shots/a/animation", json={"revision": data["revision"], "html": page("改")})
    assert saved.status_code == 200, saved.text
    detail = saved.json()
    manual = detail["plans"][p["id"]]["shots"]["a"]["animate"]
    assert manual != vid and detail["versions"][manual]["data"]["origin"] == "manual"
    assert detail["conflicts"]["render:a"]  # 渲染绑定旧动画版本 → 过期
    assert client.get(f"/api/studio/jobs/{job_id}/versions/{manual}/animation.html").text == page("改")


def wait(client, job_id, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
        if data["builds"] and data["builds"][-1]["status"] != "running":
            return data
        time.sleep(0.02)
    raise AssertionError("构建超时")


@slow
def test_five_source_mixed_compose(anim, tmp_path, video_file):
    from conftest import FixtureEmbedder
    from processing.indexing import IndexBuilder
    from processing.pipeline import Pipeline

    generator = anim.studio.video_generator
    generator.enabled, generator.backend, generator.model = True, "minimax", "fake-hailuo"
    generator.poll_interval, generator.max_wait, generator.durations = 0.01, 30, [1.0, 2.0, 3.0]
    Pipeline(anim).prepare(video_file)
    embedder = FixtureEmbedder(anim)
    IndexBuilder(anim, embedder).build(mode="visual", kinds=("shot",), activate=True)
    job_id, w, p = setup(anim, [shot("r", "红色开场。")])
    clip_id = store_input(anim, job_id, make_clip(tmp_path / "c.mp4", 2.0))
    image_id = store_input(anim, job_id, make_image(tmp_path / "i.png"))
    order = ["r", "c", "i", "g", "n"]
    ws.set_board(
        anim,
        job_id,
        w,
        p,
        [
            studio.Shot(id="r", text="红色开场。", query="红色", estimated_seconds=1),
            shot("c", "片段一段。", source={"kind": "clip", "media_id": clip_id}),
            shot("i", "图片一张。", source={"kind": "image", "media_id": image_id, "motion": "pan-left"}),
            aigc_shot("g", "生成的中段。"),
            anim_shot("n", "动画收尾。"),
        ],
    )
    ws.persist(anim, job_id, w)
    w, p = run(
        anim,
        job_id,
        w,
        p,
        animator=FakeAnimator(),
        capture=FakeCapture(),
        embedder=embedder,
        generator=FakeVideoGenerator(),
    )
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    final = ws.version(w, p["final"])
    frames = [ws.version(w, p["shots"][sid]["render"])["data"]["frames"] for sid in order]
    assert final["data"]["total_frames"] == sum(frames)
    assert [b["shot_id"] for b in final["data"]["boundaries"]] == order
    assert frame_count(ws.file_path(anim, job_id, final, "mp4")) == (sum(frames), 160, 120)
    stages = ws.detail(anim, job_id, w)["shot_stages"]
    assert [tuple(stages[sid]) for sid in order] == [
        ("recall", "tts", "select", "render"),
        ("tts", "select", "render"),
        ("tts", "render"),
        ("tts", "generate", "select", "render"),
        ("tts", "animate", "render"),
    ]


# ---------------------------------------------------------------- 设置角色


def test_settings_role_roundtrip_masks_key_and_follows_agent(config, monkeypatch):
    from settings import runtime_settings as rs
    from shared.storage import Store

    store = Store(config.data_dir)
    assert "animator" in rs.ROLES and rs.ROLE_LABELS["animator"] == "动画生成"
    assert config.studio.animator._inherited  # yaml 未写 studio.animator：复制 agent.llm
    assert rs.PROVIDER_PRESETS["anthropic"]["animator"]["config"]["backend"] == "anthropic"
    assert rs.PROVIDER_PRESETS["deepseek"]["animator"]["config"]["max_tokens"] == 16000

    # 只激活智能助手档案：动画生成跟随它（含密钥）。
    agent = rs.save_profile(
        store,
        role="agent",
        name="助手",
        provider="anthropic",
        config={"backend": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-x"},
        api_key="sk-agent-1234567890",
    )
    rs.activate_profile(store, agent["id"])
    merged = rs.apply_overrides(config, store)
    assert (merged.studio.animator.backend, merged.studio.animator.model) == ("anthropic", "claude-x")
    assert (
        merged.studio.animator.api_key == "sk-agent-1234567890" and merged.studio.animator.max_tokens == 16000
    )

    profile = rs.save_profile(
        store,
        role="animator",
        name="动画",
        provider="deepseek",
        config={
            "backend": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "max_tokens": 12000,
            "max_html_kb": 300,
        },
        api_key=SECRET,
    )
    assert profile["api_key"] == {"has_key": True, "hint": "…" + SECRET[-4:]}
    with pytest.raises(ValueError, match="不支持字段"):
        rs.save_profile(store, role="animator", name="坏", provider="custom", config={"enabled": False})
    rs.activate_profile(store, profile["id"])
    merged = rs.apply_overrides(config, store)
    animator = merged.studio.animator
    assert (animator.model, animator.max_tokens, animator.max_html_kb, animator.enabled) == (
        "deepseek-chat",
        12000,
        300,
        True,
    )
    assert animator.api_key == SECRET and SECRET not in json.dumps(merged.model_dump(mode="json"))

    client = TestClient(create_app(config))
    payload = client.get("/api/settings", params={"probe_embedding": "false"}).json()
    role = next(r for r in payload["roles"] if r["role"] == "animator")
    assert role["label"] == "动画生成" and role["effective"]["max_html_kb"] == 300
    assert SECRET not in json.dumps(payload)

    # 测试连接：一次最小对话（HTTP 桩，不调用真实模型）。
    sent = []

    def fake_request(method, url, **kwargs):
        sent.append((url, kwargs["json"], kwargs["headers"]))
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr("models.animator.adapters.request", fake_request)
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result["ok"] is True and result["reply"] == "ok", result
    url, body, headers = sent[-1]
    assert url == "https://api.deepseek.com/v1/chat/completions" and body["model"] == "deepseek-chat"
    assert headers["Authorization"] == f"Bearer {SECRET}" and body["max_tokens"] <= 1024


def test_yaml_animator_section_is_not_inherited():
    from shared.config import Config

    config = Config.model_validate(
        {
            "agent": {
                "llm": {
                    "backend": "anthropic",
                    "base_url": "https://a.example",
                    "model": "m1",
                    "api_key_env": "AGENT_KEY",
                }
            }
        }
    )
    assert (
        config.studio.animator.backend,
        config.studio.animator.model,
        config.studio.animator.api_key_env,
    ) == ("anthropic", "m1", "AGENT_KEY")
    explicit = Config.model_validate({"studio": {"animator": {"model": "own", "max_html_kb": 50}}})
    assert explicit.studio.animator.model == "own" and not explicit.studio.animator._inherited
    with pytest.raises(ValueError):
        Config.model_validate({"studio": {"animator": {"unknown": 1}}})


def test_picture_column_runs_each_shots_picture_stage(anim, tmp_path):
    """分镜工作台的“召回”列（虚拟列 picture）：每个分镜执行各自的画面阶段，合在一次列操作里；
    上传图片的分镜没有画面阶段，不参与。"""
    job_id, w, p = setup(anim, [shot("r", "素材召回。")])
    image = store_input(anim, job_id, make_image(tmp_path / "still.png"))
    ws.set_board(
        anim,
        job_id,
        w,
        p,
        [
            shot("r", "素材召回。"),
            anim_shot("a"),
            aigc_shot("g"),
            shot("i", "上传图片。", source={"kind": "image", "media_id": image}),
        ],
    )
    ws.persist(anim, job_id, w)
    assert ws.stages.picture_stage(ws.stages.plan_shot_stages(w, p, "i")) is None
    inputs = {
        sid: ws.specification(anim, job_id, w, p, stage, sid)["inputs"]
        for sid, stage in (("r", "recall"), ("a", "animate"), ("g", "generate"))
    }
    nodes = ws.column_nodes(anim, job_id, w, p, "picture", inputs)
    assert [(n["key"], n["stage"], n["shot_id"]) for n in nodes] == [
        ("recall:r", "recall", "r"),
        ("animate:a", "animate", "a"),
        ("generate:g", "generate", "g"),
    ]
    # 生成视频/动画依赖配音：还没有配音时阻塞并说明原因；召回不依赖配音。
    assert nodes[0]["action"] == "rebuild"
    assert all(n["action"] == "blocked" and "tts" in n["reason"] for n in nodes[1:])
    with pytest.raises(studio.StudioError, match="全部分镜"):
        ws.column_nodes(anim, job_id, w, p, "picture", {**inputs, "i": {"content": None}})
    with pytest.raises(studio.StudioError, match="全部分镜"):
        ws.column_nodes(anim, job_id, w, p, "picture", {"r": inputs["r"]})
    client = TestClient(
        create_app(
            anim,
            speaker=FixtureSpeaker(anim),
            animator=FakeAnimator(),
            animation_capture=FakeCapture(),
            video_generator=FakeVideoGenerator(),
        )
    )
    base = f"/api/studio/jobs/{job_id}/plans/{p['id']}"
    stale = client.post(
        f"{base}/execute-column",
        json={
            "revision": ws.revision(p),
            "stage": "picture",
            "force": False,
            "inputs_by_shot": {"r": inputs["r"]},
        },
    )
    assert stale.status_code == 422 and "全部分镜" in stale.json()["detail"]
