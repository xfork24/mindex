"""参考文稿（需求 8）：上传/粘贴登记、去重、摘要校验、总字数上限、rewrite 预检、签名与智能助手工具。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime

import pytest
from conftest import FixtureWriter
from fastapi.testclient import TestClient

from mindex.agent.attachments import AttachmentStore
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext, ToolError
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.maintenance.clean import clean_library
from mindex.studio import studio
from mindex.studio import workspace as ws

OUTLINE = "讲讲一位老匠人的一天。"
INTERVIEW = "\ufeff问：您做这行多久了？\r\n答：三十年了。\r\n"
NORMALIZED = "问：您做这行多久了？\n答：三十年了。"


@pytest.fixture
def client(config):
    writer = FixtureWriter(config)
    with TestClient(create_app(config, writer=writer)) as value:
        yield value, writer


def new_job(c, outline=OUTLINE):
    response = c.post("/api/studio/jobs", json={"text": outline, "options": {"burn": False}})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def workspace(c, jid):
    return c.get(f"/api/studio/jobs/{jid}/workspace").json()


def refs_path(jid, data):
    return f"/api/studio/jobs/{jid}/plans/{data['active_plan']}/references"


def add_text(c, jid, text, name="文稿"):
    data = workspace(c, jid)
    return c.post(refs_path(jid, data), json={"revision": data["revision"], "name": name, "text": text})


def add_file(c, jid, content: bytes, filename="访谈.txt", ctype="text/plain"):
    data = workspace(c, jid)
    return c.post(refs_path(jid, data), files={"file": (filename, content, ctype)},
                  data={"revision": data["revision"]})


def options(data):
    return data["plans"][data["active_plan"]]["options"]


def set_template(c, jid, template_id):
    data = workspace(c, jid)
    response = c.patch(f"/api/studio/jobs/{jid}/plans/{data['active_plan']}",
                       json={"revision": data["revision"], "options": {**options(data), "template_id": template_id}})
    assert response.status_code == 200, response.text
    return response.json()


def run_script(c, jid):
    data = workspace(c, jid)
    response = c.post(f"/api/studio/jobs/{jid}/plans/{data['active_plan']}/nodes",
                      json={"revision": data["revision"], "inputs": {}, "stage": "script", "force": True})
    assert response.status_code == 202, response.text
    for _ in range(2000):
        data = workspace(c, jid)
        if data["builds"] and data["builds"][-1]["status"] != "running":
            return data
        time.sleep(0.01)
    raise AssertionError("build timeout")


def clear_outline(config, jid):
    state = studio.load_job(config, jid)
    studio.save(studio.job_path(config, jid), {**state, "outline": ""})


# ---------------------------------------------------------------- 登记
def test_upload_file_and_paste_text_register_normalized_content(client, config):
    c, _ = client
    jid = new_job(c)
    uploaded = add_file(c, jid, INTERVIEW.encode("utf-8"))
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()["reference"]
    rid = hashlib.sha256(NORMALIZED.encode()).hexdigest()
    assert item == {"id": rid, "name": "访谈", "chars": len(NORMALIZED)}
    stored = ws.root(config, jid) / "inputs" / f"{rid}.txt"
    assert stored.read_bytes() == NORMALIZED.encode()  # BOM、\r\n 与首尾空白已规范化
    # 粘贴相同正文（换行风格不同）：同一 id，只登记一次。
    again = add_text(c, jid, NORMALIZED.replace("\n", "\r\n") + "  \n", name="重复")
    assert again.status_code == 201 and again.json()["reference"]["id"] == rid
    pasted = add_text(c, jid, "第二份材料。", name="  补充   材料 ")
    assert pasted.status_code == 201
    data = pasted.json()
    assert [r["name"] for r in options(data)["references"]] == ["访谈", "补充 材料"]
    assert data["reference_limit"] == config.studio.max_reference_chars
    # PATCH 方案参数（即使带着伪造的登记）不改变参考文稿登记。
    forged = {**options(data), "references": [{"id": "0" * 64, "name": "x", "chars": 1}]}
    patched = c.patch(f"/api/studio/jobs/{jid}/plans/{data['active_plan']}",
                      json={"revision": data["revision"], "options": forged}).json()
    assert options(patched)["references"] == options(data)["references"]


def test_rejects_binary_bad_type_stale_revision_and_over_limit(client, config):
    c, _ = client
    jid = new_job(c)
    binary = add_file(c, jid, b"\xff\xfe\x00\x01binary")
    assert binary.status_code == 422 and "UTF-8" in binary.json()["detail"]
    nul = add_file(c, jid, b"a\x00b")
    assert nul.status_code == 422
    wrong = add_file(c, jid, b"text", filename="a.pdf", ctype="application/pdf")
    assert wrong.status_code == 422 and ".txt" in wrong.json()["detail"]
    blank = add_text(c, jid, "  \n ")
    assert blank.status_code == 422 and "不能为空" in blank.json()["detail"]
    data = workspace(c, jid)
    stale = c.post(refs_path(jid, data), json={"revision": "old", "text": "正文"})
    assert stale.status_code == 422 and "方案已变化" in stale.json()["detail"]
    config.studio.max_reference_chars = 10
    assert add_text(c, jid, "一二三四五六").status_code == 201
    over = add_text(c, jid, "七八九十壹")
    assert over.status_code == 422
    assert over.json()["detail"] == "参考文稿总字数超过 10 字上限（登记后共 11 字），请删减后再试"
    assert over.json()["message_key"] == "ui.reference_over_limit_error"
    assert len(options(workspace(c, jid))["references"]) == 1
    assert c.post(refs_path(jid, data), json={"revision": data["revision"], "text": "x", "extra": 1}).status_code == 422


def test_delete_removes_registration_only(client, config):
    c, _ = client
    jid = new_job(c)
    rid = add_text(c, jid, "要移除的文稿。").json()["reference"]["id"]
    data = workspace(c, jid)
    path = f"{refs_path(jid, data)}/{rid}"
    assert c.delete(path, params={"revision": "old"}).status_code == 422
    removed = c.delete(path, params={"revision": data["revision"]})
    assert removed.status_code == 200, removed.text
    assert options(removed.json())["references"] == []
    assert (ws.root(config, jid) / "inputs" / f"{rid}.txt").is_file()  # 文件保留
    missing = c.delete(path, params={"revision": removed.json()["revision"]})
    assert missing.status_code == 404 and "找不到参考文稿" in missing.json()["detail"]
    # clean 不把任务 inputs/ 当作可回收产物（参考文稿与查询媒体同等保留）。
    report = clean_library(config, now=datetime(2100, 1, 1, tzinfo=UTC))
    assert "/inputs/" not in json.dumps(report, ensure_ascii=False)


# ---------------------------------------------------------------- 文案阶段
def test_rewrite_template_blocked_without_references_and_runs_with_them(client, config):
    c, writer = client
    jid = new_job(c)
    clear_outline(config, jid)  # 改写模板允许大纲为空
    data = set_template(c, jid, "interview")
    preflight = c.get(f"/api/studio/jobs/{jid}/plans/{data['active_plan']}/preflight").json()
    node = next(n for n in preflight["nodes"] if n["stage"] == "script")
    assert node["action"] == "blocked" and node["reason"] == "访谈改写模板需要参考文稿"
    assert add_file(c, jid, INTERVIEW.encode()).status_code == 201
    data = workspace(c, jid)
    preflight = c.get(f"/api/studio/jobs/{jid}/plans/{data['active_plan']}/preflight").json()
    node = next(n for n in preflight["nodes"] if n["stage"] == "script")
    assert node["action"] == "rebuild" and node["reason"] == "根据参考文稿改写文案"
    data = run_script(c, jid)
    assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
    assert writer.outlines[-1] == "" and writer.references[-1] == (NORMALIZED,)
    assert writer.templates[-1].id == "interview"
    p = data["plans"][data["active_plan"]]
    script = data["versions"][p["script"]]
    rid = hashlib.sha256(NORMALIZED.encode()).hexdigest()
    assert script["data"]["text"] == NORMALIZED
    assert script["parameters"]["references"] == [rid]
    assert [r["id"] for r in script["data"]["references"]] == [rid]


def test_outline_template_passes_references_as_supplementary_material(client, config):
    c, writer = client
    jid = new_job(c)
    add_text(c, jid, "补充：匠人住在苏州。")
    data = run_script(c, jid)
    assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
    assert writer.outlines[-1] == OUTLINE and writer.references[-1] == ("补充：匠人住在苏州。",)


def test_tampered_or_missing_reference_fails_build(client, config):
    c, _ = client
    jid = new_job(c)
    rid = add_text(c, jid, "原始文稿。").json()["reference"]["id"]
    target = ws.root(config, jid) / "inputs" / f"{rid}.txt"
    target.write_text("被改动的文稿。", encoding="utf-8")
    data = run_script(c, jid)
    assert data["builds"][-1]["status"] == "failed"
    assert "参考文稿缺失或已损坏，请重新上传" in data["builds"][-1]["nodes"][0]["error"]
    target.unlink()
    data = run_script(c, jid)
    assert "参考文稿缺失或已损坏，请重新上传" in data["builds"][-1]["nodes"][0]["error"]


def test_script_signature_tracks_reference_ids(config):
    jid = studio.create_job(config, OUTLINE)["id"]
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    none = ws.specification(config, jid, w, p, "script")
    assert "references" not in none["parameters"]  # 无参考文稿时参数与之前逐字节一致
    p["options"].pop("references")  # 没有该键的旧方案同样不加键
    assert ws.specification(config, jid, w, p, "script") == none
    first = ws.references.register(config, jid, p, "甲", "第一份。")
    one = ws.specification(config, jid, w, p, "script")
    assert one["parameters"]["references"] == [first["id"]] and one["signature"] != none["signature"]
    second = ws.references.register(config, jid, p, "乙", "第二份。")
    two = ws.specification(config, jid, w, p, "script")
    assert two["parameters"]["references"] == sorted([first["id"], second["id"]])
    assert two["signature"] != one["signature"]
    ws.references.unregister(p, second["id"])
    assert ws.specification(config, jid, w, p, "script")["signature"] == one["signature"]
    # 其他阶段与参考文稿无关。
    assert ws.settings(config, p, "tts") == ws.settings(config, {**p, "options": {**p["options"],
                                                                                   "references": []}}, "tts")


# ---------------------------------------------------------------- 智能助手
def run_tool(client, config, name, arguments):
    spec = build_registry().get(name)
    sent: list[tuple[str, str]] = []

    async def record(request):
        sent.append((request.method, request.url.path))

    async def go():
        store = AttachmentStore(config.data_dir)
        loop = MindexClient(client.app, resolve_attachment=store.resolver(session_id=None, owner="mcp"))
        loop.http.event_hooks = {"request": [record], "response": []}
        try:
            ctx = ToolContext(client=loop, page={"page": "studio"})
            parsed = spec.parse(arguments)
            preview = await spec.preview(parsed, ctx)
            return preview, await spec.execute(parsed, ctx)
        finally:
            await loop.aclose()

    preview, output = asyncio.run(go())
    return preview, output, sent


def test_agent_reference_tools_with_attachment_and_text(client, config):
    c, _ = client
    jid = new_job(c)
    registry = build_registry()
    assert registry.get("add_reference_text").risk == "write"
    assert registry.get("remove_reference_text").risk == "danger"  # 只删记录的删除工具一律 danger
    assert {registry.get(n).domain for n in ("add_reference_text", "remove_reference_text")} == {"studio"}
    uploaded = c.post("/api/agent/attachments", files={"file": ("采访稿.md", INTERVIEW.encode(), "text/markdown")})
    assert uploaded.status_code == 201, uploaded.text
    ref = uploaded.json()["id"]
    preview, output, sent = run_tool(c, config, "add_reference_text", {"job_id": jid, "attachment": ref})
    assert f"采访稿（{len(NORMALIZED)} 字）" == preview.changes[0]["after"]
    assert any("不会自动重写" in w for w in preview.warnings)
    pid = workspace(c, jid)["active_plan"]
    assert [e for e in sent if e[0] != "GET"] == [("POST", f"/api/studio/jobs/{jid}/plans/{pid}/references")]
    rid = output.value["reference"]["id"]
    assert rid == hashlib.sha256(NORMALIZED.encode()).hexdigest()
    _, pasted, _ = run_tool(c, config, "add_reference_text", {"job_id": jid, "text": "粘贴的补充。", "name": "补充"})
    assert [r["name"] for r in pasted.value["references"]] == ["采访稿", "补充"]
    with pytest.raises(ToolError, match="必须且只能提供一个"):
        build_registry().get("add_reference_text").parse({"job_id": jid})
    bad = c.post("/api/agent/attachments", files={"file": ("x.txt", b"\xff\xfe\x00", "text/plain")}).json()["id"]
    with pytest.raises(ToolError) as exc:
        run_tool(c, config, "add_reference_text", {"job_id": jid, "attachment": bad})
    assert "UTF-8" in str(exc.value)
    preview, removed, sent = run_tool(c, config, "remove_reference_text", {"job_id": jid, "reference": rid[:8]})
    assert "采访稿" in preview.changes[0]["before"]
    assert ("DELETE", f"/api/studio/jobs/{jid}/plans/{pid}/references/{rid}") in sent
    assert [r["name"] for r in removed.value["references"]] == ["补充"]
    with pytest.raises(ToolError) as exc:
        run_tool(c, config, "remove_reference_text", {"job_id": jid, "reference": "不存在"})
    assert exc.value.code == "unresolved_reference"
