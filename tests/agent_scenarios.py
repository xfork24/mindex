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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conftest import FixtureEmbedder, FixtureSpeaker, FixtureStoryboarder, legacy_job

from mindex.agent.llm.scripted import call, say
from mindex.agent.runtime import NO_VISION_PROMPT, NO_VISION_TOOL_NOTE
from mindex.api import create_app
from mindex.processing.indexing import IndexBuilder
from mindex.processing.pipeline import Pipeline
from mindex.shared.config import Config
from mindex.shared.storage import Store, dumps
from mindex.studio import studio
from mindex.studio import studio_workspace as ws

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


def png(color: str) -> bytes:
    from PIL import Image

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

    def app(self, llm=None):
        app = create_app(self.config, self.embedder, storyboarder=FixtureStoryboarder(self.config),
                         speaker=FlakySpeaker(self.config), agent_llm=llm)
        app.state.agent_runtime.watcher.interval = 0.05
        return app


def board(config, job_id, texts, ids):
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(config, job_id, w, p, [
        studio.Shot(id=sid, text=text, query=text[:2], estimated_seconds=1.0) for sid, text in zip(ids, texts)
    ])
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return p["id"]


def build_world(config: Config, video: Path) -> World:
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.preset = "ultrafast"
    # 嵌入身份探测（设置页嵌入状态、激活预览）走真实适配器：指向本机关闭端口，立即失败而不是等待默认局域网地址超时。
    config.embedding.base_url = "http://127.0.0.1:9"
    config.embedding.timeout = 5
    source_id = Pipeline(config).prepare(video)
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot", "moment"), activate=True)
    job = legacy_job(config, "红色开场。蓝色中段。蓝色延续。绿色结尾。", options={"burn": False}, title="颜色短片")["id"]
    plan = board(config, job, ["红色开场。", "蓝色中段。", "蓝色延续。", "绿色结尾。"], SHOTS)
    broken = legacy_job(config, "红色开场。配音故障。", options={"burn": False}, title="故障任务")["id"]
    board(config, broken, ["红色开场。", "配音故障。"], ["shot-0b0b0b0b0a", "shot-0b0b0b0b0b"])
    world = World(config, embedder, job, plan, "", broken, "", source_id)
    from fastapi.testclient import TestClient

    with TestClient(world.app()) as client:
        data = client.get(f"/api/studio/jobs/{job}/workspace").json()
        other = client.post(f"/api/studio/jobs/{job}/plans", json={"name": "备选方案", "copy_from": plan}).json()
        world.other_plan = other["active_plan"]
        client.post(f"/api/studio/jobs/{job}/plans/{plan}/activate")
        bdata = client.get(f"/api/studio/jobs/{broken}/workspace").json()
        token = client.get(f"/api/studio/jobs/{broken}/plans/{bdata['active_plan']}/preflight").json()["token"]
        build = client.post(f"/api/studio/jobs/{broken}/plans/{bdata['active_plan']}/build", json={"token": token}).json()
        deadline = time.monotonic() + 120
        while client.get(f"/api/studio/jobs/{broken}/builds/{build['id']}/logs").json()["status"] == "running":
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
    return [e["data"] for e in events if e["type"] == "tool/result" and e["data"]["message"]["toolCallId"] in ids]


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
    results = [e for e in events if e["type"] == "tool/result" and (e["data"].get("meta") or {}).get("kind") == "search"]
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
    ask = call("ask_user_question", questions=[{"id": "conn", "question": "请提供自建模型的连接信息", "fields": [
        {"id": "base_url", "label": "Endpoint", "required": True},
        {"id": "model", "label": "模型名", "required": True},
        {"id": "api_key", "label": "API Key", "type": "secret"}]}])

    def create(request):
        values = json.loads(last_tool(request))["answers"][0]["values"]
        return [call("create_model_profile", role="generator", name="自建拆分镜", provider="self",
                     config={"backend": "openai", "base_url": values["base_url"], "model": values["model"],
                             "llama_template": True}, api_key=values["api_key"])]

    def activate(request):
        profile = int(re.search(r"档案 #(\d+)", last_tool(request)).group(1))
        return [call("activate_model_profile", profile_id=profile)]

    # 未给页面上下文：先加载 settings 领域，再使用设置工具。
    return [[call("load_tools", domains=["settings"])], [call("list_provider_presets", role="generator"), ask],
            create, activate, [say("已切换到自建模型。")]]


def merge_script(world):
    return [
        [call("resolve_shots", job_id=world.job, refs=["925281d5bb", "925281d6"])],
        [call("edit_shots", job_id=world.job, operations=[{"op": "merge", "shots": ["925281d5bb", "925281d6"]}])],
        [call("run_build", job_id=world.job)],
        [say("已提交构建。")],
        [say("构建完成，合并后的分镜已重建，其余复用。")],
    ]


def ambiguous_script(world):
    ask = call("ask_user_question", questions=[{"id": "which", "question": "925281d 对应两个分镜，要删除哪一个？",
                                                "options": [{"label": "#2 shot-925281d5"}, {"label": "#3 shot-925281d6"}]}])

    def delete(request):
        selected = json.loads(last_tool(request))["answers"][0]["selected"][0]
        return [call("edit_shots", job_id=world.job, operations=[{"op": "delete", "shot": selected.split()[0]}])]

    return [[call("resolve_shots", job_id=world.job, refs=["925281d"])], [ask], delete, [say("已删除。")]]


# ------------------------------------------------------------ 能力扩展场景：准备数据
def setup_index_versions(world):
    """在初始索引之上再建一个更新的活动版本，供回退。"""
    store = Store(world.config.data_dir)
    older = store.active_index()["id"]
    newer = IndexBuilder(world.config, world.embedder).build(source_id=world.source_id, channels=["visual"],
                                                             activate=True)
    assert newer != older and store.active_index()["id"] == newer
    world.extra.update(older=older, newer=newer)


def setup_slow_job(world):
    """第二个分镜的配音会一直等到有人请求取消构建，保证取消落在执行中途。"""
    job = legacy_job(world.config, "红色开场。慢速配音。", options={"burn": False}, title="慢速任务")["id"]
    world.extra.update(slow_job=job, slow_plan=board(world.config, job, ["红色开场。", "慢速配音。"], SLOW_SHOTS))


def setup_gpu_busy(world):
    """模拟另一个 GPU 任务独占 pipeline 锁：发布任务停在 GPU 准入等待（queued），可被确定地取消。"""
    from mindex.shared.processing_lock import SharedFileLock

    world.extra["gpu_holder"] = SharedFileLock(world.config.data_dir / "pipeline.lock", exclusive=True).acquire()
    world.extra["index_before"] = Store(world.config.data_dir).active_index()["id"]


def setup_persons(world):
    """一部作品、一名演员与其角色，以及首个镜头上一条待确认的出镜证据。"""
    from mindex.persons.person_models import Entity
    from mindex.persons.persons import Persons

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
        ("ev-scenario", world.source_id, shot["id"], actor, role, shot["start"], shot["end"], "candidate",
         dumps({"score": 0.82})),
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
    misses = [e for e in events if e["type"] == "tool/result"
              and (e["data"].get("error") or {}).get("code") == "TOOL_NOT_LOADED"]
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
    user = next(e["data"] for e in events if e["type"] == "user/message" and e["data"]["source"]["kind"] == "user")
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
    assert finished and finished[-1]["status"] == "cancelled" and finished[-1]["build_id"] == build["id"], finished
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
        assert finished and finished[-1]["task_id"] == task_id and finished[-1]["status"] == "cancelled", finished
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
    assert any(any(s.get("role_id") == world.extra["role"] for s in c.get("selections") or [])
               for c in calls_of(events, "review_shot")), "审核必须指定角色"
    assert ok_results(events, "list_review_shots") and ok_results(events, "review_shot")
    assert ok_results(events, "publish_persons"), "没有发布人物检索"
    asked = {a["name"]: a["risk"] for a in (e["data"] for e in events if e["type"] == "plugin:mindex/approval-asked")}
    assert asked.get("review_shot") == "write" and asked.get("publish_persons") == "danger", asked
    release = Store(world.config.data_dir).active_index()["search_release_id"]
    assert release and release != world.extra["release_before"], "发布后应切换到新的检索组合"


def check_vision(world, client, events):
    user = next(e["data"] for e in events if e["type"] == "user/message" and e["data"]["source"]["kind"] == "user")
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
    return [lambda request: [call("search_by_media", attachment=attachment_ref(request), granularity="shot",
                                  limit=3)],
            [say("最相似的是红色开场镜头。")]]


def similar_script(world):
    def similar(request):
        seed = re.search(r"^#1 .* 片段 (\S+)", last_tool(request), re.M).group(1)
        return [call("find_similar", segment_id=seed, limit=5)]

    return [[call("search_media", text="绿色", granularity="shot", limit=3)], similar,
            [say("这些是与该绿色镜头画面相似的片段。")]]


def index_rollback_script(world):
    older = world.extra["older"]
    return [[call("load_tools", domains=["index"])], [call("list_index_versions")],
            [call("check_index", index_id=older)], [call("activate_index", index_id=older)],
            [say("已回退到上一个索引版本。")]]


def cancel_build_script(world):
    job = world.extra["slow_job"]
    return [[call("run_build", job_id=job)], [call("cancel_build", job_id=job)],
            [say("已请求取消，正在执行的节点完成后停止。")], [say("构建已取消，已生成的版本会保留。")]]


def cancel_task_script(world):
    def cancel(request):
        task = re.search(r"素材任务 (\w+)", last_tool(request)).group(1)
        return [call("cancel_task", kind="source", task_id=task)]

    return [[call("start_source_task", source_id=world.source_id, work="publish", target="shots")], cancel,
            [say("已取消该发布任务。")], [say("发布任务已取消，活动索引未改变；需要时可用 retry_task 重试。")]]


def persons_publish_script(world):
    selection = {"actor_id": world.extra["actor"], "role_id": world.extra["role"]}
    return [[call("list_review_shots", status="candidate")],
            [call("review_shot", source_id=world.source_id, shot_id=world.extra["shot"], selections=[selection])],
            [call("publish_persons")], [say("已确认出镜并发布人物检索。")]]


def vision_script(world):
    return [[call("load_tools", domains=["search"])],
            [call("sample_frames", source_id=world.source_id, start=0, end=2, count=3)],
            [say("截图主要是红色；开头三帧依次是红色、蓝色和绿色画面。")]]


def no_vision_script(world):
    return [[call("load_tools", domains=["search"])],
            [call("sample_frames", source_id=world.source_id, start=0, end=2, count=3)],
            [say("已抽取 3 帧，画面在面板展示。")]]


SCENARIOS = [
    Scenario("custom_model", "我想使用自定义模型配置：拆分镜改用我自建的模型服务", check_custom_model, custom_model_script,
             form={"url": "http://127.0.0.1:9/v1", "endpoint": "http://127.0.0.1:9/v1", "model": "qwen3-self",
                   "key": SECRET, "secret": SECRET}),
    Scenario("split_granularity", "我要更改文案拆分镜的粒度，拆粗一点", check_gap, lambda w: [
        [say("目前拆分镜粒度没有可调参数；可以用 edit_shots 手动合并相邻分镜。"),
         call("report_capability_gap", request="调整拆分镜粒度", reason="粒度写死在提示词里",
              suggestion="手动合并分镜")], [say("已记录。")]]),
    Scenario("merge_rebuild", "合并 925281d5bb 和 925281d6 两个分镜，并重新生成该分镜的后续流程", check_merge_build,
             merge_script, page=lambda w: {"page": "studio", "job_id": w.job, "plan_id": w.plan}),
    Scenario("switch_plan", "把当前方案切换到「备选方案」", check_switch_plan, lambda w: [
        [call("get_workspace_summary", job_id=w.job)], [call("activate_plan", job_id=w.job, plan_id=w.other_plan)],
        [say("已切换。")]], page=lambda w: {"page": "studio", "job_id": w.job}),
    Scenario("change_voice", "把当前方案的配音音色改成 Ethan", check_voice, lambda w: [
        [call("update_plan_options", job_id=w.job, changes={"voice": "Ethan"})], [say("已修改。")]],
        page=lambda w: {"page": "studio", "job_id": w.job, "plan_id": w.plan}),
    Scenario("diagnose_build", "「故障任务」最近一次构建为什么失败？", check_diagnose, lambda w: [
        [call("load_tools", domains=["studio"])], [call("list_jobs")],
        [call("get_build_status", job_id=w.broken_job)],
        lambda request: [say("失败原因：" + re.search(r"(TTS[^\n]*)", last_tool(request)).group(1))]]),
    Scenario("search", "帮我找一段绿色画面的素材", check_search, lambda w: [
        [call("search_media", text="绿色 画面")], [say("找到了这些片段。")]], page=lambda w: {"page": "search"}),
    Scenario("ambiguous_id", "删除分镜 925281d", check_ambiguous, ambiguous_script, choose="#2",
             page=lambda w: {"page": "studio", "job_id": w.job}),
    Scenario("rejected", "删除「颜色短片」这个创作任务", check_rejected, lambda w: [
        [call("load_tools", domains=["studio"])], [call("delete_job", job_id=w.job)],
        [say("好的，已取消删除。")]], approve="reject"),
    Scenario("secret", "我要换一个自建的拆分镜模型，地址和密钥我来提供", check_secret, custom_model_script,
             form={"url": "http://127.0.0.1:9/v1", "endpoint": "http://127.0.0.1:9/v1", "model": "qwen3-self",
                   "key": SECRET, "secret": SECRET}),
    # ---------------- 能力扩展：动态加载、附件、找相似、索引、取消、人物、视觉
    Scenario("cross_domain_load", "帮我找一段蓝色画面的素材；另外看看现在有哪些内容索引版本，人物库里登记了哪些人物。",
             check_cross_domain, cross_domain_script, page=lambda w: {"page": "studio", "job_id": w.job},
             note="创作页出发跨三个领域：未加载即调用得到 TOOL_NOT_LOADED 后自行 load_tools；已加载领域不超过 3 个"),
    Scenario("attachment_search", "用我上传的这张截图，在素材库里找画面相似的镜头", check_attachment_search,
             attachment_search_script, page=lambda w: {"page": "search"}, attachments=red_attachment,
             note="面板图片附件 → search_by_media(attachment=…)；红色截图应命中红色开场"),
    Scenario("similar_search", "找一个绿色画面的镜头，再找几个和它画面相似的片段", check_similar, similar_script,
             page=lambda w: {"page": "search"}, note="search_media 的结果片段作为种子调用 find_similar"),
    Scenario("index_rollback", "最新的内容索引有问题，帮我回退到上一个索引版本；回退前先检查一下那个版本是否完好",
             check_index_rollback, index_rollback_script, page=lambda w: {"page": "settings"},
             setup=setup_index_versions, note="list_index_versions → check_index → activate_index（danger 确认卡）"),
    Scenario("cancel_build", "先开始构建「慢速任务」的成片，开始后马上帮我取消这次构建（我只是确认取消流程）",
             check_cancel_build, cancel_build_script,
             page=lambda w: {"page": "studio", "job_id": w.extra["slow_job"], "plan_id": w.extra["slow_plan"]},
             setup=setup_slow_job, note="run_build 后 cancel_build（write）；构建以 cancelled 结束并收到 followup"),
    Scenario("cancel_source_task", "给这个素材发起一次分镜结果的检索发布任务，提交后立刻帮我取消它（我只是确认取消流程）",
             check_cancel_task, cancel_task_script, page=admin_page, setup=setup_gpu_busy,
             note="start_source_task（danger，GPU 被占用而排队）→ cancel_task（write）→ task-finished followup"),
    Scenario("persons_review_publish", "出镜审核里有个待确认的镜头，画面里是演员甲饰演的叶淑柔；帮我确认，然后发布人物检索",
             check_persons_publish, persons_publish_script, page=lambda w: {"page": "persons"},
             setup=setup_persons, note="list_review_shots → review_shot（write）→ publish_persons（danger）"),
    Scenario("vision", "我上传了一张截图，看看它主要是什么颜色；再从这个素材开头 2 秒抽 3 帧给我看", check_vision,
             vision_script, page=admin_page, setup=setup_vision, attachments=red_attachment, needs=("vision",),
             note="视觉开启：用户图片附件与 sample_frames 画面都以图片进入模型请求（需支持图片输入的模型）"),
    Scenario("no_vision", "从这个素材开头 2 秒抽 3 帧画面给我看看", check_no_vision, no_vision_script, page=admin_page,
             note="视觉关闭：不发送图片，工具结果注明仅在面板展示，系统提示说明图片未发送"),
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


WATCH_PAIRS = (("plugin:mindex/build-watch", "plugin:mindex/build-finished"),
               ("plugin:mindex/task-watch", "plugin:mindex/task-finished"))


def drive(client, scenario: Scenario, world: World, *, timeout: float = 180) -> list[dict]:
    """发送请求并自动答复确认与提问，直到会话空闲且没有在跟踪的构建或后台任务。"""
    sid = client.post("/api/agent/sessions", json={}).json()["id"]
    context = scenario.page(world) if scenario.page else None
    refs = []
    for name, data, content_type in (scenario.attachments(world) if scenario.attachments else []):
        response = client.post("/api/agent/attachments", files={"file": (name, data, content_type)},
                               data={"session_id": sid})
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
                client.post(f"/api/agent/sessions/{sid}/approvals/{item['requestId']}", json={"outcome": outcome})
            else:
                client.post(f"/api/agent/sessions/{sid}/questions/{item['requestId']}",
                            json={"answers": [answer_form(scenario, q) for q in item["questions"]]})
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
