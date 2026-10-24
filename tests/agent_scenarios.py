"""智能助手验收场景：同一组场景既用脚本化模型做确定性测试，也由 scripts/agent_eval.py 交给真实模型评测。

每个场景给出：用户请求、页面上下文、确认与提问的自动答复策略、按最终状态判定的检查函数，
以及确定性测试用的脚本（真实模型评测时忽略脚本）。检查只看结果状态，不要求固定的调用顺序。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conftest import FixtureEmbedder, FixtureSpeaker, FixtureStoryboarder, legacy_job

from mindex.agent.llm.scripted import call, say
from mindex.api import create_app
from mindex.processing.indexing import IndexBuilder
from mindex.processing.pipeline import Pipeline
from mindex.shared.config import Config
from mindex.studio import studio
from mindex.studio import studio_workspace as ws

SHOTS = ["shot-a1daad59aa", "shot-925281d5bb", "shot-925281d6cc", "shot-77f0c3e1dd"]
SECRET = "sk-scenario-PLAINTEXT-987654321zz"


class FlakySpeaker(FixtureSpeaker):
    """测试专用：旁白含「故障」时模拟配音服务不可用。"""

    def speak(self, text, voice=None):
        if "故障" in text:
            raise RuntimeError("TTS 服务不可用：连接被拒绝")
        return super().speak(text, voice)


@dataclass
class World:
    config: Config
    embedder: Any
    job: str
    plan: str
    other_plan: str
    broken_job: str
    broken_build: str

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
    Pipeline(config).prepare(video)
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot", "moment"), activate=True)
    job = legacy_job(config, "红色开场。蓝色中段。蓝色延续。绿色结尾。", options={"burn": False}, title="颜色短片")["id"]
    plan = board(config, job, ["红色开场。", "蓝色中段。", "蓝色延续。", "绿色结尾。"], SHOTS)
    broken = legacy_job(config, "红色开场。配音故障。", options={"burn": False}, title="故障任务")["id"]
    board(config, broken, ["红色开场。", "配音故障。"], ["shot-0b0b0b0b0a", "shot-0b0b0b0b0b"])
    world = World(config, embedder, job, plan, "", broken, "")
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

    return [[call("list_provider_presets", role="generator"), ask], create, activate, [say("已切换到自建模型。")]]


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
        [call("list_jobs")], [call("get_build_status", job_id=w.broken_job)],
        lambda request: [say("失败原因：" + re.search(r"(TTS[^\n]*)", last_tool(request)).group(1))]]),
    Scenario("search", "帮我找一段绿色画面的素材", check_search, lambda w: [
        [call("search_media", text="绿色 画面")], [say("找到了这些片段。")]], page=lambda w: {"page": "search"}),
    Scenario("ambiguous_id", "删除分镜 925281d", check_ambiguous, ambiguous_script, choose="#2",
             page=lambda w: {"page": "studio", "job_id": w.job}),
    Scenario("rejected", "删除「颜色短片」这个创作任务", check_rejected, lambda w: [
        [call("delete_job", job_id=w.job)], [say("好的，已取消删除。")]], approve="reject"),
    Scenario("secret", "我要换一个自建的拆分镜模型，地址和密钥我来提供", check_secret, custom_model_script,
             form={"url": "http://127.0.0.1:9/v1", "endpoint": "http://127.0.0.1:9/v1", "model": "qwen3-self",
                   "key": SECRET, "secret": SECRET}),
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


def drive(client, scenario: Scenario, world: World, *, timeout: float = 180) -> list[dict]:
    """发送请求并自动答复确认与提问，直到会话空闲且没有在跟踪的构建。"""
    sid = client.post("/api/agent/sessions", json={}).json()["id"]
    context = scenario.page(world) if scenario.page else None
    client.post(f"/api/agent/sessions/{sid}/messages", json={"content": scenario.prompt, "context": context})
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
        watching = types.count("plugin:mindex/build-watch") > types.count("plugin:mindex/build-finished")
        idle = session["status"] == "idle" and types.count("turn/start") == types.count("turn/end") and types
        if idle and not watching:
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since > 0.3:
                return events
        else:
            quiet_since = None
        time.sleep(0.05)
    raise AssertionError(f"场景 {scenario.name} 超时")
