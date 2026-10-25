"""阶段注册表：处理器登记、按镜头来源的阶段序列，以及新增阶段/来源无需改动预检与执行。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_studio_workspace import setup_workspace

from mindex.api import create_app
from mindex.studio import studio, studio_models
from mindex.studio import workspace as ws
from mindex.studio.workspace import stages


def test_registry_lists_exactly_the_stages_in_order():
    assert list(ws.REGISTRY) == ["script", "split", "recall", "tts", "music", "draw", "generate", "animate", "invoke",
                                 "select", "overlay", "render", "compose", "export", "metadata", "cover", "package"]
    for name, item in ws.REGISTRY.items():
        assert isinstance(item, ws.StageHandler)
        assert item.name == name and item.label
        assert item.scope in ("plan", "shot", "final")
    assert stages.names(scope="plan") == ("script", "split")
    # 逐镜阶段按注册顺序；generate 只属于 aigc 来源、animate 只属于 animation 来源，recall 来源的序列（STAGES）不变。
    assert stages.names(scope="shot") == ("recall", "tts", "music", "draw", "generate", "animate", "invoke", "select",
                                          "overlay", "render")
    assert ws.STAGES == ("recall", "tts", "select", "render")
    assert stages.names(scope="final") == ("compose", "export", "metadata", "cover", "package")
    assert {name: item.label for name, item in ws.REGISTRY.items()} == {
        "script": "生成文案", "split": "拆分镜", "recall": "召回", "tts": "配音", "music": "背景音乐",
        "draw": "生成图片", "generate": "生成画面",
        "animate": "生成动画", "invoke": "调用第三方工具", "select": "选片与对齐", "overlay": "生成叠加层", "render": "分镜渲染", "compose": "成片合成",
        "export": "多画幅导出", "metadata": "发布元数据", "cover": "封面", "package": "发布包",
    }


def test_shot_stages_default_to_recall_source():
    assert ws.shot_stages({}) == ws.STAGES
    assert ws.shot_stages({"text": "旁白", "query": "夜景"}) == ws.STAGES
    assert ws.shot_stages({"source": {"kind": "recall"}}) == ws.STAGES
    with pytest.raises(studio.StudioError, match="未知的镜头来源"):
        ws.shot_stages({"source": {"kind": "missing"}})


def test_request_models_accept_registry_stages():
    assert studio_models.VERSION_STAGES == ("script", "content", "recall", "tts", "music", "draw", "generate", "animate",
                                            "invoke", "select", "overlay", "render", "compose", "export", "metadata",
                                            "cover", "package")
    assert studio_models.NODE_STAGES == ("script", "split", "recall", "tts", "music", "draw", "generate", "animate",
                                         "invoke", "select", "overlay", "render")
    # picture：分镜表“召回”列的虚拟列，覆盖各分镜的画面阶段（recall/draw/generate/animate）。
    assert studio_models.COLUMN_STAGES == ("split", "recall", "tts", "music", "draw", "generate", "animate", "invoke",
                                           "select", "overlay", "render", "picture")


class FakeHandler(ws.StageHandler):
    name, label, scope = "fake", "虚构阶段", "shot"

    def __init__(self):
        self.runs = []

    def settings(self, config, p, *, store=None, display=False):
        return {"fake_version": 1}

    def specification(self, config, job_id, w, p, sid, params, resources):
        selected, content = stages.shot_content(w, p, sid)
        return {"text": content["text"]}, {"content": selected["content"]}, params

    def run(self, ctx):
        self.runs.append(ctx.sid)
        target = ctx.directory / "fake.txt"
        target.write_text("ok", encoding="utf-8")
        return {"ok": True}, {"fake": ws.file_info(ctx.config, ctx.job_id, target)}


def test_registered_stage_and_source_extend_preflight_and_execute(config, monkeypatch):
    fake = FakeHandler()
    monkeypatch.setitem(ws.REGISTRY, "fake", fake)
    monkeypatch.setitem(ws.SHOT_SOURCES, "fake", ("fake", "tts", "render"))
    jid, w, p = setup_workspace(config)
    # 分镜 a 改为虚构来源；b 仍是缺省 recall 来源。
    w["versions"][p["shots"]["a"]["content"]]["data"]["source"] = {"kind": "fake"}

    keys = [n["key"] for n in ws.preflight(config, jid, w, p)["nodes"]]
    assert "fake:a" in keys and "fake:b" not in keys
    assert not any(key in keys for key in ("recall:a", "select:a"))
    assert {"recall:b", "tts:b", "select:b", "render:b", "tts:a", "render:a"} <= set(keys)
    # 依赖按来源计算：没有选片阶段的分镜，渲染只依赖配音。
    assert ws.node_dependencies(w, p, "render", "a") == ["tts:a"]
    assert ws.node_dependencies(w, p, "render", "b") == ["select:b", "tts:b"]

    vid = ws.execute(config, jid, w, p, "fake", "a")
    assert fake.runs == ["a"]
    item = ws.version(w, vid)
    assert item["stage"] == "fake" and item["status"] == "complete" and item["data"] == {"ok": True}
    assert item["parameters"] == {"fake_version": 1}
    assert p["shots"]["a"]["fake"] == vid
    assert ws.match(config, jid, w, p, "fake", "a") == vid
    nodes = {n["key"]: n for n in ws.preflight(config, jid, w, p)["nodes"]}
    assert nodes["fake:a"]["action"] == "reuse" and nodes["fake:a"]["version"] == vid


def test_unknown_stage_rejected(config):
    jid, w, p = setup_workspace(config)
    with pytest.raises(studio.StudioError, match="未知阶段"):
        ws.specification(config, jid, w, p, "fake", "a")


def test_invalid_stage_in_request_bodies_is_422(config):
    jid, _, p = setup_workspace(config)
    client = TestClient(create_app(config))
    base = f"/api/studio/jobs/{jid}/plans/{p['id']}"
    node = {"revision": ws.revision(p), "inputs": {}, "shot_id": "a"}
    for stage in ("fake", "compose", "content"):
        response = client.post(f"{base}/nodes", json={**node, "stage": stage})
        assert response.status_code == 422, (stage, response.text)
    column = {"revision": ws.revision(p), "inputs_by_shot": {}}
    for stage in ("script", "compose"):
        response = client.post(f"{base}/regenerate-column", json={**column, "stage": stage})
        assert response.status_code == 422, (stage, response.text)
    choice = {"revision": ws.revision(p), "version_id": "x", "stage": "split"}
    response = client.post(f"{base}/select-version", json=choice)
    assert response.status_code == 422, response.text
    # OpenAPI 中的阶段仍是字符串枚举，取值与注册表一致。
    for body, values in ((studio_models.NodeBody, studio_models.NODE_STAGES),
                         (studio_models.ColumnBody, studio_models.COLUMN_STAGES),
                         (studio_models.VersionChoiceBody, studio_models.VERSION_STAGES)):
        stage = body.model_json_schema()["properties"]["stage"]
        assert stage == {"enum": list(values), "title": "Stage", "type": "string"}
