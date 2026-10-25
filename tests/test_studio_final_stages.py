"""多个成片阶段与变体（spec m3-post-production 需求 10）：注册表里 compose 之后的成片阶段按变体展开节点、
预检草稿占位让下游看到尚未构建的 compose、链式复用、阻塞传播与 ``p["finals"]`` 选用；只有 compose 的方案行为不变。"""

from __future__ import annotations

import pytest
from test_studio_sources import build, make_image, setup, shot, small, store_input  # noqa: F401 — small 是夹具

from mindex.studio import studio
from mindex.studio import workspace as ws
from mindex.studio.workspace import stages


class ProbeHandler(stages.StageHandler):
    """Tests only: 依赖 compose 的成片阶段，每个变体写一份记录 compose 版本的文本。"""

    name, label, scope = "probe", "测试成片阶段", "final"
    buildable = column = False

    def __init__(self, variants=("a", "b")):
        self.wanted, self.runs = list(variants), []

    def variants(self, config, w, p):
        return list(self.wanted)

    def settings(self, config, p, *, store=None, display=False):
        return {"probe": 1}

    def specification(self, config, job_id, w, p, sid, params, resources):
        inputs = {"compose": p.get("final")}
        return {"compose": inputs["compose"], "variant": sid}, inputs, params

    def dependencies(self, w, p, sid, *, resplit=False):
        return ["split"] if resplit else ["compose"]

    def run(self, ctx):
        self.runs.append(ctx.sid)
        target = ctx.directory / "probe.txt"
        studio.save_bytes(target, f"{ctx.sid}:{ctx.p['final']}".encode())
        return {"variant": ctx.sid}, {"probe": ws.file_info(ctx.config, ctx.job_id, target)}


@pytest.fixture
def probe(monkeypatch):
    handler = ProbeHandler()
    monkeypatch.setitem(stages.REGISTRY, "probe", handler)
    return handler


def image_plan(config, tmp_path):
    job_id, w, p = setup(config, [shot("a", "红色开场。")])
    image = store_input(config, job_id, make_image(tmp_path / "i.png"))
    ws.set_board(config, job_id, w, p, [shot("a", "红色开场。", source={"kind": "image", "media_id": image})])
    ws.persist(config, job_id, w)
    return job_id, w, p


def test_selected_and_assign_keep_old_plans_untouched():
    compose = stages.handler("compose")
    p = {"final": "v1"}
    assert compose.selected(p, None) == "v1"
    compose.assign(p, None, "v2")
    assert p == {"final": "v2"}  # compose 仍写 p["final"]，不引入 finals 键
    assert stages.final_key("export", "9:16") == "export:9:16" and stages.parse_final_key("export:9:16") == (
        "export", "9:16")
    assert stages.parse_final_key("compose") == ("compose", None)


def test_preflight_expands_variants_with_placeholders(small, tmp_path, probe):  # noqa: F811
    job_id, w, p = image_plan(small, tmp_path)
    nodes = ws.preflight(small, job_id, w, p)["nodes"]
    finals = [n for n in nodes if n["stage"] in ("compose", "probe")]
    assert [(n["key"], n["shot_id"], n["action"]) for n in finals] == [
        ("compose", None, "rebuild"), ("probe:a", "a", "rebuild"), ("probe:b", "b", "rebuild")]
    assert ws.node_dependencies(w, p, "probe", "a") == ["compose"]
    # 草稿占位：probe 的签名看到的是尚未构建的 compose 占位，而不是空。
    draft = {**p, "final": "@compose"}
    spec = ws.specification(small, job_id, w, draft, "probe", "a")
    assert spec["inputs"] == {"compose": "@compose"}


@pytest.mark.slow
def test_chained_build_reuse_choose_and_blocking(small, tmp_path, probe):  # noqa: F811
    job_id, w, p = image_plan(small, tmp_path)
    w, p = build(small, job_id, w, p)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    assert sorted(probe.runs) == ["a", "b"]
    assert set(p["finals"]) == {"probe:a", "probe:b"}
    made = ws.version(w, p["finals"]["probe:a"])
    assert made["shot_id"] == "a" and made["inputs"] == {"compose": p["final"]}
    assert ws.file_path(small, job_id, made, "probe").read_text() == f"a:{p['final']}"
    # 再次预检：全部复用；去掉一个变体后只剩一个节点。
    assert {n["key"]: n["action"] for n in ws.preflight(small, job_id, w, p)["nodes"]}["probe:b"] == "reuse"
    probe.wanted = ["a"]
    assert [n["key"] for n in ws.preflight(small, job_id, w, p)["nodes"] if n["stage"] == "probe"] == ["probe:a"]
    # 选用：final 变体版本写进 finals。
    other = ws.add_version(small, job_id, w, "probe", "a", data={}, inputs={"compose": p["final"]})
    ws.choose(w, p, "probe", "a", other)
    assert p["finals"]["probe:a"] == other
    with pytest.raises(studio.StudioError):
        ws.choose(w, p, "probe", "b", other)  # 版本属于变体 a
    # 冲突：上游 compose 变化后，probe 报告上游已变化。
    ws.choose(w, p, "probe", "a", made["id"])
    p["final"] = ws.add_version(small, job_id, w, "compose", data={}, inputs={})
    messages = ws.conflicts(small, job_id, w, p, "probe", "a", made["id"])
    assert any("依赖 compose" in item for item in messages)
    # 阻塞传播：compose 被阻塞时 probe 也阻塞。
    nodes = ws.preflight(small, job_id, w, p)["nodes"]
    for node in nodes:
        if node["key"] == "compose":
            node["action"] = "blocked"
    blocked = {n["key"] for n in nodes if n["action"] == "blocked"}
    for node in nodes:
        if any(dep in blocked for dep in ws.node_dependencies(w, p, node["stage"], node["shot_id"])):
            blocked.add(node["key"])
    assert "probe:a" in blocked


def test_plan_with_only_compose_has_no_finals_key(small, tmp_path):  # noqa: F811
    job_id, w, p = image_plan(small, tmp_path)
    assert "finals" not in p
    nodes = ws.preflight(small, job_id, w, p)["nodes"]
    assert [n["key"] for n in nodes if n["shot_id"] is None or n["stage"] == "compose"][-1] == "compose"
