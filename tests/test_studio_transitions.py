"""分镜转场（spec m3-post-production 需求 3、4）：镜内转场 enter/leave 与重叠转场 next。

- 模型：None 与空转场不落键，旧 content 版本号不变；
- 签名：没有转场时 render/compose 签名与旧版一致；镜内转场只重建该镜渲染与成片；重叠转场影响本镜渲染（余量）与成片；
- 构建：成片总帧数 = 各镜 frames 之和，boundaries 不变；镜内黑场首帧变暗；上传片段的余量从源继续取。
"""

from __future__ import annotations

import subprocess

import pytest
from pydantic import ValidationError
from test_studio_sources import (
    build,
    frame_count,
    make_clip,
    make_image,
    setup,
    shot,
    small,  # noqa: F401 — small 是夹具
    store_input,
)

from mindex.studio import workspace as ws
from mindex.studio.workspace import transitions

slow = pytest.mark.slow


def luma(path, frame: int) -> float:
    log = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(path), "-vf",
         f"select='eq(n\\,{frame})',signalstats,metadata=print:key=lavfi.signalstats.YAVG", "-f", "null", "-"],
        capture_output=True, text=True, check=True).stderr
    return float(next(line for line in log.splitlines() if "YAVG=" in line).split("YAVG=")[1])


def test_transition_model_dump_and_validation():
    plain = shot("a", "旁白")
    assert "transition" not in plain.model_dump()
    assert shot("a", "旁白", transition={}).transition is None
    assert shot("a", "旁白", transition={"enter": None}).model_dump() == plain.model_dump()
    edged = shot("a", "旁白", transition={"enter": {"kind": "fade"}, "next": {"kind": "wipeleft", "duration": 1}})
    assert edged.model_dump()["transition"] == {"enter": {"kind": "fade", "duration": 0.5},
                                                "next": {"kind": "wipeleft", "duration": 1.0}}
    with pytest.raises(ValidationError):
        shot("a", "旁白", transition={"enter": {"kind": "spin"}})
    with pytest.raises(ValidationError):
        shot("a", "旁白", transition={"next": {"duration": 3}})


def test_effective_rules():
    def plan(*contents):
        w = {"versions": {}}
        p = {"board": "b", "shots": {}}
        order = []
        for index, content in enumerate(contents):
            sid = "abc"[index]
            w["versions"][f"c{sid}"] = {"data": {"transition": content} if content else {}}
            p["shots"][sid] = {"content": f"c{sid}"}
            order.append(sid)
        w["versions"]["b"] = {"data": {"order": order}}
        return w, p

    fade = {"kind": "fade", "duration": 0.5}
    w, p = plan({"leave": fade, "next": {"kind": "fade", "duration": 0.4}}, {"enter": fade}, {"next": {"duration": 1}})
    first, second, last = (transitions.effective(w, p, sid) for sid in "abc")
    assert "leave" not in first and first["next"]["duration"] == 0.4 and first["ignored"]
    assert "enter" not in second and second["ignored"]
    assert "next" not in last and last["ignored"] == ["最后一镜没有下一镜，重叠转场已忽略"]
    assert transitions.render_payload(w, p, "c") is None
    assert transitions.overlaps(w, p) == [["a", {"kind": "fade", "duration": 0.4}]]
    assert transitions.handle_seconds(w, p, "a") == 0.4 and transitions.handle_seconds(w, p, "b") == 0
    enter, leave, notes = transitions.clamp_edges({"enter": {"kind": "fade", "duration": 2}}, 1.0)
    assert enter["duration"] == 0.5 and leave is None and notes


def test_signatures_only_change_where_transitions_apply(small, tmp_path):  # noqa: F811
    job_id, w, p = setup(small, [shot("a", "红色开场。"), shot("b", "蓝色结尾。")])
    image = store_input(small, job_id, make_image(tmp_path / "i.png"))
    board = [shot("a", "红色开场。", source={"kind": "image", "media_id": image}),
             shot("b", "蓝色结尾。", source={"kind": "image", "media_id": image})]
    ws.set_board(small, job_id, w, p, board)

    def signatures():
        return {key: ws.specification(small, job_id, w, p, stage, sid)["signature"]
                for key, stage, sid in (("a", "render", "a"), ("b", "render", "b"), ("final", "compose", None))}

    before = signatures()
    def compose_payload():
        return ws.stages.handler("compose").specification(small, job_id, w, p, None, {}, None)[0]

    assert "transitions" not in compose_payload()
    ws.set_board(small, job_id, w, p, [
        shot("a", "红色开场。", source={"kind": "image", "media_id": image}, transition={"enter": {"kind": "fade"}}),
        board[1]])
    edged = signatures()
    assert edged["a"] != before["a"] and edged["b"] == before["b"]
    ws.set_board(small, job_id, w, p, [
        shot("a", "红色开场。", source={"kind": "image", "media_id": image}, transition={"next": {"kind": "fade"}}),
        board[1]])
    overlapped = signatures()
    assert overlapped["a"] not in (before["a"], edged["a"]) and overlapped["b"] == before["b"]
    assert compose_payload()["transitions"] == [
        ["a", {"kind": "fade", "duration": 0.5}]]
    # 最后一镜的重叠转场被忽略：签名不变。
    ws.set_board(small, job_id, w, p, [
        board[0], shot("b", "蓝色结尾。", source={"kind": "image", "media_id": image}, transition={"next": {}})])
    assert signatures() == before


@slow
def test_build_with_edge_and_overlap_keeps_timeline(small, tmp_path):  # noqa: F811
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    image = store_input(small, job_id, make_image(tmp_path / "i.png"))
    clip = store_input(small, job_id, make_clip(tmp_path / "c.mp4", 6.0, color="white"))
    plain = [shot("a", "红色开场画面。", source={"kind": "image", "media_id": image}),
             shot("b", "白色片段结尾。", source={"kind": "clip", "media_id": clip}),
             shot("c", "收尾。", source={"kind": "image", "media_id": image})]
    ws.set_board(small, job_id, w, p, plain)
    w, p = build(small, job_id, w, p)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    hard = ws.version(w, p["final"])

    ws.set_board(small, job_id, w, p, [
        shot("a", "红色开场画面。", source={"kind": "image", "media_id": image},
             transition={"enter": {"kind": "fade", "duration": 0.4}}),
        shot("b", "白色片段结尾。", source={"kind": "clip", "media_id": clip},
             transition={"next": {"kind": "dissolve", "duration": 0.4}}),
        plain[2]])
    ws.persist(small, job_id, w)
    w, p = build(small, job_id, w, p)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    nodes = {n["key"]: n["status"] for n in w["builds"][-1]["nodes"]}
    assert nodes["render:a"] == "complete" and nodes["render:b"] == "complete" and nodes["render:c"] == "reused"
    a, b = (ws.version(w, p["shots"][sid]["render"]) for sid in ("a", "b"))
    assert "handle_frames" not in a["data"] and b["data"]["handle_frames"] == 10
    assert frame_count(ws.file_path(small, job_id, b, "video"))[0] == b["data"]["frames"] + 10
    assert luma(ws.file_path(small, job_id, a, "video"), 0) < 30  # 黑场淡入
    final = ws.version(w, p["final"])
    assert final["data"]["boundaries"] == hard["data"]["boundaries"]
    assert final["data"]["total_frames"] == hard["data"]["total_frames"]
    mp4 = ws.file_path(small, job_id, final, "mp4")
    assert frame_count(mp4)[0] == final["data"]["total_frames"]
    # 交叉过渡从 b→c 的切点开始：b 的尾部余量在 c 开头的 0.4 秒内与 c 交叉，切点前仍是 b 的白色画面。
    cut = final["data"]["boundaries"][1]["frames"] + final["data"]["boundaries"][0]["frames"]
    after = luma(mp4, cut + 12)
    assert luma(mp4, cut - 1) > 230 and after < 120
    assert after < luma(mp4, cut + 5) < 230
