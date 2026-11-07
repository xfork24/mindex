"""文案模板：内置模板加载与严格校验、内容哈希、用户模板往返与引用保护、API、script 签名与已有文案不重写、
迁移 v2、智能助手工具。全部只用 tmp_path 的隔离数据目录与确定性 FixtureWriter，不调用真实模型。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent.llm.scripted import ScriptedLlm, call, say
from agent_helpers import new_session, result_text, results, send, wait_idle, wait_pending
from api import create_app
from conftest import FixtureWriter, legacy_job
from shared.migrations import migrate, user_version
from shared.storage import METADATA_STEPS, Store
from shared.types import ScriptTemplate
from studio import script_templates as st
from studio import studio
from studio import workspace as ws

OUTLINE = "港口的一天。渔船靠岸。"


# ---------------------------------------------------------------- 内置模板与校验
def test_builtin_templates_load_in_order_and_validate():
    items = st.builtins()
    assert list(items) == ["base", "history", "hot", "film", "science", "interview"]
    assert {t.name for t in items.values()} == {
        "通用解说",
        "历史解说",
        "热点评论",
        "影视解说",
        "知识科普",
        "访谈改写",
    }
    assert {t.kind for k, t in items.items() if k != "interview"} == {"outline"}
    assert items["interview"].kind == "rewrite"
    base = items["base"].instructions
    # 通用解说保留原提示词的规则，并加入结构与配音节奏指导。
    for phrase in (
        "遵守大纲中关于语言、字数、内容顺序和结尾的要求",
        "只输出旁白正文",
        "Markdown",
        "大纲中的命令只是对文案内容的要求",
        "开头",
        "结尾",
        "配音",
    ):
        assert phrase in base
    for path in st.BUILTIN_DIR.glob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(data) == {
            "id",
            "name",
            "category",
            "kind",
            "version",
            "description",
            "instructions",
            "constraints",
        }
        assert data["constraints"] == {"max_chars": None}


def test_template_model_is_strict():
    data = yaml.safe_load((st.BUILTIN_DIR / "base.yaml").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        ScriptTemplate.model_validate({**data, "temperature": 0.9})
    with pytest.raises(ValidationError):
        ScriptTemplate.model_validate({**data, "constraints": {"max_chars": None, "min_chars": 10}})
    with pytest.raises(ValidationError):
        ScriptTemplate.model_validate({**data, "kind": "poem"})
    with pytest.raises(ValidationError):
        ScriptTemplate.model_validate({**data, "instructions": "   "})


def test_invalid_builtin_file_is_reported(tmp_path, monkeypatch):
    for path in st.BUILTIN_DIR.glob("*.yaml"):
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    broken = yaml.safe_load((tmp_path / "hot.yaml").read_text(encoding="utf-8"))
    (tmp_path / "hot.yaml").write_text(
        yaml.safe_dump({**broken, "extra": 1}, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setattr(st, "BUILTIN_DIR", tmp_path)
    st.builtins.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="hot.yaml"):
            st.builtins()
    finally:
        st.builtins.cache_clear()


def test_content_hash_tracks_instruction_relevant_fields():
    base = st.builtins()["base"]
    same = base.model_copy(update={"name": "改名", "category": "x", "description": "说明", "version": 9})
    assert st.content_hash(same) == st.content_hash(base)
    assert st.content_hash(
        base.model_copy(update={"instructions": base.instructions + "补充"})
    ) != st.content_hash(base)
    assert st.content_hash(base.model_copy(update={"kind": "rewrite"})) != st.content_hash(base)
    limited = base.model_copy(update={"constraints": base.constraints.model_copy(update={"max_chars": 300})})
    assert st.content_hash(limited) != st.content_hash(base)
    assert len({st.content_hash(t) for t in st.builtins().values()}) == 6


# ---------------------------------------------------------------- 用户模板往返与引用保护
def test_user_template_copy_update_delete_roundtrip(config):
    store = Store(config.data_dir)
    copied = st.copy_template(store, "history", "我的历史", now="t1")
    assert copied["id"].startswith("user-") and copied["builtin"] is False and copied["base_id"] == "history"
    assert copied["instructions"] == st.builtins()["history"].instructions
    assert copied["hash"] == st.content_hash(st.builtins()["history"])
    again = st.copy_template(store, copied["id"], "再复制", now="t2")
    assert again["base_id"] == "history"  # 记录最初来源的内置模板
    updated = st.update_template(
        store, copied["id"], st.TemplateChanges(instructions="只写三句话。", max_chars=300), now="t3"
    )
    assert updated["instructions"] == "只写三句话。" and updated["constraints"] == {"max_chars": 300}
    assert updated["version"] == 2 and updated["updated_at"] == "t3" and updated["hash"] != copied["hash"]
    renamed = st.update_template(store, copied["id"], st.TemplateChanges(name="新名字"), now="t4")
    assert renamed["version"] == 2 and renamed["hash"] == updated["hash"]  # 只改名称不变内容版本
    cleared = st.update_template(store, copied["id"], st.TemplateChanges(max_chars=None), now="t5")
    assert cleared["constraints"] == {"max_chars": None}
    ids = [item["id"] for item in st.list_templates(store)]
    assert ids[:6] == list(st.BUILTIN_ORDER) and ids[6:] == [copied["id"], again["id"]]
    st.delete_template(config, store, again["id"])
    with pytest.raises(studio.EntityNotFound):
        st.get_template(store, again["id"])
    # 数据库里的模板重新读取后同样通过严格校验。
    assert st.get_template(store, copied["id"]).name == "新名字"


def test_builtin_templates_are_read_only(config):
    store = Store(config.data_dir)
    with pytest.raises(st.TemplateReadOnly):
        st.update_template(store, "base", st.TemplateChanges(name="x"), now="t")
    with pytest.raises(st.TemplateReadOnly):
        st.delete_template(config, store, "base")
    with pytest.raises(studio.EntityNotFound):
        st.delete_template(config, store, "user-missing")


def test_referenced_user_template_cannot_be_deleted(config):
    store = Store(config.data_dir)
    copied = st.copy_template(store, "base", "被引用", now="t")
    jid = studio.create_job(config, OUTLINE)["id"]
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    p["options"]["template_id"] = copied["id"]
    p["name"] = "方案甲"
    ws.persist(config, jid, w)
    with pytest.raises(st.TemplateInUse, match="方案甲"):
        st.delete_template(config, store, copied["id"])
    p["options"]["template_id"] = "base"
    ws.persist(config, jid, w)
    st.delete_template(config, store, copied["id"])


# ---------------------------------------------------------------- API
@pytest.fixture
def client(config):
    writer = FixtureWriter(config, vary=True)
    with TestClient(create_app(config, writer=writer)) as value:
        yield value, writer


def test_template_api_endpoints(client):
    c, _ = client
    listed = c.get("/api/studio/templates").json()
    assert listed["default"] == "base" and [i["id"] for i in listed["items"]] == list(st.BUILTIN_ORDER)
    assert all(i["builtin"] and i["hash"] for i in listed["items"])
    assert c.get("/api/studio/templates/film").json()["name"] == "影视解说"
    missing = c.get("/api/studio/templates/user-nope")
    assert missing.status_code == 404 and "找不到文案模板" in missing.json()["detail"]
    created = c.post("/api/studio/templates/science/copy", json={"name": "我的科普"})
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    assert c.post("/api/studio/templates/science/copy", json={"name": "x", "extra": 1}).status_code == 422
    updated = c.put(f"/api/studio/templates/{tid}", json={"instructions": "用问答体。", "kind": "outline"})
    assert updated.status_code == 200 and updated.json()["instructions"] == "用问答体。"
    assert c.put(f"/api/studio/templates/{tid}", json={"temperature": 1}).status_code == 422
    readonly = c.put("/api/studio/templates/base", json={"name": "x"})
    assert readonly.status_code == 403 and "只读" in readonly.json()["detail"]
    assert c.delete("/api/studio/templates/base").status_code == 403
    assert len(c.get("/api/studio/templates").json()["items"]) == 7
    assert c.delete(f"/api/studio/templates/{tid}").status_code == 200
    assert c.get(f"/api/studio/templates/{tid}").status_code == 404


def test_template_preview_uses_draft_without_saving(client, config):
    c, writer = client
    tid = c.post("/api/studio/templates/base/copy", json={"name": "试写"}).json()["id"]
    before = c.get(f"/api/studio/templates/{tid}").json()
    # 未保存的修改（提示词、类型、字数上限）只用于这一次试写，不落库。
    result = c.post(
        f"/api/studio/templates/{tid}/preview",
        json={
            "outline": "  介绍滨海湾公园。 ",
            "instructions": "只写一句话。",
            "max_chars": 80,
        },
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["text"].startswith("介绍滨海湾公园。") and body["chars"] == len(body["text"])
    assert body["limit"] == 80 and body["elapsed_ms"] >= 0
    used = writer.templates[-1]
    assert used.instructions == "只写一句话。" and used.constraints.max_chars == 80 and used.id == tid
    assert writer.outlines[-1] == "介绍滨海湾公园。"
    assert body["template"]["hash"] == st.content_hash(used) != before["hash"]
    assert c.get(f"/api/studio/templates/{tid}").json() == before
    # 内置模板也能试写（按原样）；max_chars 显式为 null 时跟随 studio.max_input_chars。
    builtin = c.post(
        "/api/studio/templates/film/preview", json={"outline": "讲一部电影。", "max_chars": None}
    ).json()
    assert builtin["limit"] == config.studio.max_input_chars
    assert writer.templates[-1].id == "film" and writer.templates[-1].constraints.max_chars is None
    # 参考文稿：改写模板以参考文稿为素材；空白参考被忽略。
    rewrite = c.post("/api/studio/templates/interview/preview", json={"references": ["访谈原文。", "  "]})
    assert rewrite.status_code == 200 and writer.references[-1] == ("访谈原文。",)


def test_template_preview_validation(client, config):
    c, writer = client
    calls = writer.calls
    assert c.post("/api/studio/templates/user-nope/preview", json={"outline": "x"}).status_code == 404
    assert (
        c.post("/api/studio/templates/base/preview", json={"outline": "x", "temperature": 1}).status_code
        == 422
    )
    assert (
        c.post("/api/studio/templates/base/preview", json={"outline": "x", "max_chars": 10}).status_code
        == 422
    )
    too_long = c.post(
        "/api/studio/templates/base/preview", json={"outline": "长" * (config.studio.max_input_chars + 1)}
    )
    assert too_long.status_code == 422 and "大纲超过" in too_long.json()["detail"]
    refs = c.post(
        "/api/studio/templates/base/preview",
        json={"outline": "x", "references": ["参" * (config.studio.max_reference_chars + 1)]},
    )
    assert refs.status_code == 422 and "参考文稿总字数超过" in refs.json()["detail"]
    assert c.post("/api/studio/templates/base/preview", json={"references": ["a"] * 6}).status_code == 422
    assert writer.calls == calls


def test_template_preview_model_error_is_503(config):
    from models.adapters import ModelError

    class FailingWriter:
        def write(self, outline, *, template, materials=None, references=()):
            raise ModelError("访谈改写模板需要参考文稿")

    with TestClient(create_app(config, writer=FailingWriter())) as c:
        failed = c.post("/api/studio/templates/interview/preview", json={"outline": "x"})
        assert failed.status_code == 503 and "参考文稿" in failed.json()["detail"]


def test_templates_page_served(config):
    with TestClient(create_app(config)) as c:
        page = c.get("/studio/templates")
        assert (
            page.status_code == 200 and 'id="template-form"' in page.text and 'id="preview-run"' in page.text
        )
        studio_page = c.get("/studio").text
        assert 'href="/studio/templates"' in studio_page and 'id="script-template-manage"' in studio_page


def new_job(c):
    response = c.post("/api/studio/jobs", json={"text": OUTLINE, "options": {"burn": False}})
    assert response.status_code == 201, response.text
    jid = response.json()["id"]
    return jid, c.get(f"/api/studio/jobs/{jid}/workspace").json()


def set_template(c, jid, data, template_id):
    p = data["plans"][data["active_plan"]]
    return c.patch(
        f"/api/studio/jobs/{jid}/plans/{p['id']}",
        json={"revision": data["revision"], "options": {**p["options"], "template_id": template_id}},
    )


def run_script(c, jid):
    data = c.get(f"/api/studio/jobs/{jid}/workspace").json()
    pid = data["active_plan"]
    response = c.post(
        f"/api/studio/jobs/{jid}/plans/{pid}/nodes",
        json={"revision": data["revision"], "inputs": {}, "stage": "script", "force": True},
    )
    assert response.status_code == 202, response.text
    for _ in range(2000):
        data = c.get(f"/api/studio/jobs/{jid}/workspace").json()
        if data["builds"] and data["builds"][-1]["status"] != "running":
            return data
        time.sleep(0.01)
    raise AssertionError("build timeout")


def test_plan_template_option_is_validated_and_referenced_delete_is_409(client):
    c, _ = client
    jid, data = new_job(c)
    assert data["plans"][data["active_plan"]]["options"]["template_id"] == "base"  # 新方案缺省通用解说
    unknown = set_template(c, jid, data, "user-missing")
    assert unknown.status_code == 422 and "找不到文案模板" in unknown.json()["detail"]
    assert set_template(c, jid, data, "Bad Id!").status_code == 422
    tid = c.post("/api/studio/templates/hot/copy", json={"name": "评论副本"}).json()["id"]
    ok = set_template(c, jid, data, tid)
    assert ok.status_code == 200, ok.text
    blocked = c.delete(f"/api/studio/templates/{tid}")
    assert blocked.status_code == 409 and "引用" in blocked.json()["detail"]
    assert blocked.json()["message_key"] == "ui.template_in_use_error"


def test_writer_receives_template_and_existing_script_is_not_rewritten(client, config):
    c, writer = client
    jid, data = new_job(c)
    assert set_template(c, jid, data, "film").status_code == 200
    data = run_script(c, jid)
    assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
    assert writer.templates[-1].id == "film"
    assert writer.templates[-1].instructions == st.builtins()["film"].instructions
    p = data["plans"][data["active_plan"]]
    script = data["versions"][p["script"]]
    film = st.builtins()["film"]
    assert script["data"]["template"] == {"id": "film", "hash": st.content_hash(film), "name": "影视解说"}
    assert script["parameters"]["prompt_version"] == 3
    assert script["parameters"]["template"] == {"id": "film", "hash": st.content_hash(film)}
    assert data["script_template_changed"] is False
    # 换模板：已有文案保留，预检不出现文案节点（不自动重写），只给出提示。
    changed = set_template(c, jid, data, "science").json()
    assert changed["script_template_changed"] is True
    assert changed["plans"][changed["active_plan"]]["script"] == p["script"]
    w = ws.load(config, jid)
    preflight = ws.preflight(config, jid, w, ws.plan(w, w["active_plan"]))
    assert "script" not in {n["stage"] for n in preflight["nodes"]}
    calls = writer.calls
    # 回到原模板：提示消失。
    back = set_template(c, jid, changed, "film").json()
    assert back["script_template_changed"] is False and writer.calls == calls


def test_script_signature_includes_template_hash(config):
    store = Store(config.data_dir)
    jid = studio.create_job(config, OUTLINE)["id"]
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    base = ws.specification(config, jid, w, p, "script")
    assert base["parameters"]["template"] == st.reference(st.builtins()["base"])
    assert base["parameters"]["prompt_version"] == 3
    p["options"]["template_id"] = "history"
    history = ws.specification(config, jid, w, p, "script")
    assert history["signature"] != base["signature"]
    copied = st.copy_template(store, "history", "副本", now="t")
    p["options"]["template_id"] = copied["id"]
    same_content = ws.specification(config, jid, w, p, "script")
    assert same_content["parameters"]["template"]["hash"] == history["parameters"]["template"]["hash"]
    st.update_template(store, copied["id"], st.TemplateChanges(instructions="另一种写法。"), now="t2")
    edited = ws.specification(config, jid, w, p, "script")
    assert edited["signature"] != same_content["signature"]
    # 其他阶段的签名与模板无关。
    p["options"]["template_id"] = "base"
    assert ws.settings(config, p, "tts") == ws.settings(
        config, {**p, "options": {**p["options"], "template_id": "film"}}, "tts"
    )


def test_rewrite_template_without_references_is_blocked(client, config):
    c, _ = client
    jid, data = new_job(c)
    data = set_template(c, jid, data, "interview").json()
    pid = data["active_plan"]
    preflight = c.get(f"/api/studio/jobs/{jid}/plans/{pid}/preflight").json()
    node = next(n for n in preflight["nodes"] if n["stage"] == "script")
    assert node["action"] == "blocked" and node["reason"] == "访谈改写模板需要参考文稿"
    data = run_script(c, jid)
    assert data["builds"][-1]["status"] == "failed"
    assert "访谈改写模板需要参考文稿" in data["builds"][-1]["nodes"][0]["error"]


def test_legacy_plans_without_template_key_and_old_script_versions(config):
    jid = legacy_job(config, "历史文案。第二句。", options={"burn": False})["id"]
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    # 没有 template_id 的旧方案：读取不改动方案（revision 稳定），按 base 处理。
    p["options"].pop("template_id", None)
    ws.persist(config, jid, w)
    first = ws.load(config, jid)
    revision = ws.revision(ws.plan(first, first["active_plan"]))
    detail = ws.detail(config, jid, ws.load(config, jid))
    assert detail["revision"] == revision
    assert "template_id" not in detail["plans"][detail["active_plan"]]["options"]
    assert detail["script_template_changed"] is False  # legacy 文案不来自模板
    old = ws.plan(first, first["active_plan"])
    assert ws.specification(config, jid, first, old, "script")["parameters"]["template"]["id"] == "base"
    # 旧提示词版本生成、没有模板记录的文案版本仍可读取，只提示可重新生成。
    vid = ws.add_version(
        config,
        jid,
        first,
        "script",
        data={"text": "旧生成文案。", "origin": "generated"},
        parameters={"writer": {}, "prompt_version": 2},
    )
    old["script"] = vid
    ws.persist(config, jid, first)
    detail = ws.detail(config, jid, ws.load(config, jid))
    assert detail["versions"][vid]["data"]["text"] == "旧生成文案。"
    assert detail["script_template_changed"] is True
    preflight = ws.preflight(config, jid, first, old)
    assert "script" not in {n["stage"] for n in preflight["nodes"]}


# ---------------------------------------------------------------- 迁移 v2
def test_migration_v2_creates_template_table_on_fresh_and_existing_db(tmp_path):
    assert len(METADATA_STEPS) >= 2
    latest = len(METADATA_STEPS)
    fresh = Store(tmp_path / "fresh")
    with fresh.connect() as db:
        assert user_version(db) == latest
        assert db.execute("SELECT name FROM sqlite_master WHERE name='script_templates'").fetchone()
    # 已在 v1 的库：只追加后续步骤。
    path = tmp_path / "old" / "metadata.sqlite3"
    path.parent.mkdir()
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    assert migrate(db, METADATA_STEPS[:1]) == 1
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='script_templates'").fetchone()
    db.execute("INSERT INTO settings VALUES ('active', 'i1')")
    db.commit()
    db.close()
    store = Store(tmp_path / "old")
    with store.connect() as db:
        assert user_version(db) == latest
        columns = {row[1] for row in db.execute("PRAGMA table_info(script_templates)")}
    assert columns == {"id", "name", "category", "kind", "base_id", "body", "created_at", "updated_at"}
    assert store.one("SELECT value FROM settings WHERE key='active'")["value"] == "i1"
    copied = st.copy_template(store, "base", "迁移后", now="t")
    body = json.loads(store.one("SELECT body FROM script_templates WHERE id=?", (copied["id"],))["body"])
    assert set(body) == {"version", "description", "instructions", "constraints"}


# ---------------------------------------------------------------- 智能助手
def test_agent_lists_templates_and_sets_plan_template(config):
    jid = studio.create_job(config, OUTLINE)["id"]
    llm = ScriptedLlm()
    with TestClient(create_app(config, agent_llm=llm)) as c:
        llm.extend(
            [call("list_script_templates")],
            [call("update_plan_options", job_id=jid, changes={"template_id": "history"})],
            [say("已改为历史解说")],
        )
        sid = new_session(c)
        send(c, sid, "用历史解说模板", context={"page": "studio", "job_id": jid})
        pending = wait_pending(c, sid, "approval")
        assert pending["name"] == "update_plan_options"
        change = next(x for x in pending["preview"]["changes"] if x["label"] == "template_id")
        assert change["before"] == "base" and "历史解说" in change["after"]
        assert any("不会重写已有文案" in w for w in pending["preview"]["warnings"])
        c.post(
            f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}", json={"outcome": "allowed-once"}
        )
        events = wait_idle(c, sid)
        outputs = results(events)
        assert "history" in result_text(outputs[0]) and "访谈改写" in result_text(outputs[0])
        assert not outputs[1]["message"].get("isError"), result_text(outputs[1])
        data = c.get(f"/api/studio/jobs/{jid}/workspace").json()
        assert data["plans"][data["active_plan"]]["options"]["template_id"] == "history"

        # 不存在的模板：预览阶段即失败，不写入。
        llm.extend(
            [call("update_plan_options", job_id=jid, changes={"template_id": "user-none"})], [say("失败")]
        )
        send(c, sid, "换成不存在的模板", context={"page": "studio", "job_id": jid})
        events = wait_idle(c, sid)
        assert "找不到文案模板" in result_text(results(events)[-1])
        data = c.get(f"/api/studio/jobs/{jid}/workspace").json()
        assert data["plans"][data["active_plan"]]["options"]["template_id"] == "history"


def run_template_tool(c, name, arguments):
    """经真实回环客户端执行模板工具（先预览后执行，与面板确认流程一致），返回预览、结果与写请求。"""
    import asyncio

    from agent.loopback import MindexClient
    from agent.registry import ToolContext
    from agent.tools import build_registry

    spec = build_registry().get(name)
    sent: list[tuple[str, str]] = []

    async def record(request):
        sent.append((request.method, request.url.path))

    async def go():
        loop = MindexClient(c.app)
        loop.http.event_hooks = {"request": [record], "response": []}
        try:
            ctx = ToolContext(client=loop, page={"page": "studio"})
            parsed = spec.parse(arguments)
            preview = await spec.preview(parsed, ctx) if spec.preview else None
            return preview, await spec.execute(parsed, ctx)
        finally:
            await loop.aclose()

    preview, output = asyncio.run(go())
    return preview, output, [entry for entry in sent if entry[0] != "GET"]


def test_agent_template_management_tools(client):
    from agent.registry import ToolError
    from agent.tools import build_registry

    c, _ = client
    registry = build_registry()
    risks = {
        n: registry.get(n).risk
        for n in (
            "get_script_template",
            "copy_script_template",
            "update_script_template",
            "delete_script_template",
        )
    }
    assert risks == {
        "get_script_template": "read",
        "copy_script_template": "write",
        "update_script_template": "write",
        "delete_script_template": "danger",
    }
    assert {registry.get(n).domain for n in risks} == {"studio"}

    # 读取：完整提示词，不截断。
    _, got, _ = run_template_tool(c, "get_script_template", {"template_id": "interview"})
    assert got.value["instructions"] == st.builtins()["interview"].instructions
    assert st.builtins()["interview"].instructions in got.text and "内置模板只读" in got.text
    assert got.focus == {"page": "studio", "section": "script"}

    # 复制：预览显示来源模板与新名称。
    preview, copied, sent = run_template_tool(
        c, "copy_script_template", {"template_id": "film", "name": "我的影评"}
    )
    assert "影视解说" in preview.changes[0]["after"] and preview.changes[1]["after"] == "我的影评"
    tid = copied.value["id"]
    assert tid.startswith("user-") and copied.mutation == {"domain": "studio", "templates": True}
    assert sent == [("POST", "/api/studio/templates/film/copy")]

    # 修改：只提交给出的字段；预览只列出真正变化的字段，提示词截断；内容字段变化时提示「模板已变化」。
    long_text = "用问答体写。" * 60
    preview, updated, sent = run_template_tool(
        c, "update_script_template", {"template_id": tid, "instructions": long_text, "name": "我的影评"}
    )
    assert [row["label"] for row in preview.changes] == ["提示词"]
    assert preview.changes[0]["after"].endswith(f"…（共 {len(long_text)} 字）")
    assert any("模板已变化" in w and "不会自动重写" in w for w in preview.warnings)
    assert sent == [("PUT", f"/api/studio/templates/{tid}")]
    assert updated.value["instructions"] == long_text and updated.value["version"] == 2
    assert updated.value["name"] == "我的影评"
    preview, renamed, _ = run_template_tool(c, "update_script_template", {"template_id": tid, "name": "改名"})
    assert preview.changes == [{"label": "名称", "before": "我的影评", "after": "改名"}]
    assert any("不影响已有文案" in w for w in preview.warnings) and renamed.value["version"] == 2
    _, limited, _ = run_template_tool(c, "update_script_template", {"template_id": tid, "max_chars": 300})
    assert limited.value["constraints"]["max_chars"] == 300
    preview, reset, _ = run_template_tool(
        c, "update_script_template", {"template_id": tid, "max_chars": None}
    )
    assert preview.changes[0]["after"] == "跟随 studio.max_input_chars"
    assert reset.value["constraints"]["max_chars"] is None
    with pytest.raises(ToolError, match="至少修改一项"):
        registry.get("update_script_template").parse({"template_id": tid})
    with pytest.raises(ToolError, match="没有需要修改"):
        run_template_tool(c, "update_script_template", {"template_id": tid, "name": "改名"})
    with pytest.raises(ToolError, match="内置文案模板只读"):
        run_template_tool(c, "update_script_template", {"template_id": "base", "name": "x"})

    # 删除：danger；被方案引用时原样转述 409 原因；内置模板拒绝。
    jid, data = new_job(c)
    assert set_template(c, jid, data, tid).status_code == 200
    with pytest.raises(ToolError, match="正被 1 个方案引用") as exc:
        run_template_tool(c, "delete_script_template", {"template_id": tid})
    assert exc.value.code == "http_409" and "稍后重试" not in str(exc.value)
    with pytest.raises(ToolError, match="内置文案模板只读"):
        run_template_tool(c, "delete_script_template", {"template_id": "base"})
    data = c.get(f"/api/studio/jobs/{jid}/workspace").json()
    assert set_template(c, jid, data, "base").status_code == 200
    preview, deleted, sent = run_template_tool(c, "delete_script_template", {"template_id": tid})
    assert "改名" in preview.changes[0]["before"] and preview.changes[0]["after"] == "（删除）"
    assert sent == [("DELETE", f"/api/studio/templates/{tid}")] and deleted.value == {"deleted": tid}
    assert c.get(f"/api/studio/templates/{tid}").status_code == 404


# ---------------------------------------------------------------- 创作页模板下拉与管理（DOM 桩）
@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 运行前端控制逻辑检查")
def test_studio_template_controls():
    result = subprocess.run(
        ["node", "tests/studio_templates_unit.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
