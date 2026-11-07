(
    """智能助手 / MCP 的转场入口（spec m3-post-production 需求 3、4、22）：edit_shots """
    """设置、清除转场，合并与拆分规则。"""
)

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.registry import ToolError
from agent.tools.studio_write import merge_transition, split_transition
from api import create_app
from conftest import FixtureSpeaker
from test_agent_aigc import content
from test_agent_studio_recall import run
from test_studio_sources import setup, shot, small  # noqa: F401 — small 是夹具

A, B = "shot-a1daad59aa", "shot-925281d5bb"


@pytest.fixture
def agent_job(small):  # noqa: F811
    job_id, _, _ = setup(small, [shot(A, "红色开场。"), shot(B, "蓝色结尾。")])
    with TestClient(create_app(small, speaker=FixtureSpeaker(small))) as client:
        yield small, client, job_id


def test_edit_shots_sets_and_clears_transition(agent_job):
    config, client, job_id = agent_job
    preview, output, _ = run(
        client,
        config,
        "edit_shots",
        {
            "job_id": job_id,
            "operations": [
                {
                    "op": "update",
                    "shot": "#1",
                    "fields": {
                        "transition": {
                            "enter": {"kind": "fade", "duration": 0.4},
                            "next": {"kind": "dissolve"},
                        }
                    },
                }
            ],
        },
    )
    change = next(c for c in preview.changes if "转场" in c["label"])
    assert change["before"] == "硬切（无转场）"
    assert "入场黑场 0.4 秒" in change["after"] and "重叠转场 dissolve 0.5 秒" in change["after"]
    assert content(client, job_id, A)["transition"] == {
        "enter": {"kind": "fade", "duration": 0.4},
        "next": {"kind": "dissolve", "duration": 0.5},
    }
    assert "转场：" in output.text
    run(
        client,
        config,
        "edit_shots",
        {"job_id": job_id, "operations": [{"op": "update", "shot": "#1", "clear": ["transition"]}]},
    )
    assert "transition" not in content(client, job_id, A)
    with pytest.raises(Exception, match="kind"):
        run(
            client,
            config,
            "edit_shots",
            {
                "job_id": job_id,
                "operations": [
                    {"op": "update", "shot": "#1", "fields": {"transition": {"enter": {"kind": "spin"}}}}
                ],
            },
        )


def test_empty_transition_is_hard_cut(agent_job):
    config, client, job_id = agent_job
    run(
        client,
        config,
        "edit_shots",
        {"job_id": job_id, "operations": [{"op": "update", "shot": "#2", "fields": {"transition": {}}}]},
    )
    assert "transition" not in content(client, job_id, B)
    with pytest.raises(ToolError):
        run(
            client,
            config,
            "edit_shots",
            {
                "job_id": job_id,
                "operations": [
                    {
                        "op": "update",
                        "shot": "#2",
                        "fields": {"transition": {"enter": {"kind": "fade"}}},
                        "clear": ["transition"],
                    }
                ],
            },
        )


def test_highlights_and_subtitle_style(agent_job):
    config, client, job_id = agent_job
    preview, _, _ = run(
        client,
        config,
        "edit_shots",
        {
            "job_id": job_id,
            "operations": [
                {"op": "update", "shot": "#1", "fields": {"highlights": ["红色", "红色", " 开场 "]}}
            ],
        },
    )
    assert next(c for c in preview.changes if "高亮" in c["label"])["after"] == "红色、开场"
    assert content(client, job_id, A)["highlights"] == ["红色", "开场"]
    run(
        client,
        config,
        "edit_shots",
        {"job_id": job_id, "operations": [{"op": "update", "shot": "#1", "clear": ["highlights"]}]},
    )
    assert "highlights" not in content(client, job_id, A)
    preview, output, _ = run(
        client,
        config,
        "update_plan_options",
        {"job_id": job_id, "changes": {"burn": True, "subtitle_style": "highlight"}},
    )
    assert any("只重新合成成片" in item for item in preview.warnings)
    assert output.value["options"]["subtitle_style"] == "highlight"
    _, output, _ = run(
        client, config, "update_plan_options", {"job_id": job_id, "changes": {"subtitle_style": "default"}}
    )
    assert output.value["options"].get("subtitle_style") is None


def test_overlays_via_edit_shots(agent_job):
    config, client, job_id = agent_job
    preview, output, _ = run(
        client,
        config,
        "edit_shots",
        {
            "job_id": job_id,
            "operations": [
                {
                    "op": "update",
                    "shot": "#2",
                    "fields": {
                        "overlays": [
                            {"kind": "lower_third", "title": "张三", "subtitle": "研究员"},
                            {
                                "kind": "data_card",
                                "items": [{"label": "增长", "value": "35%"}],
                                "engine": "html",
                            },
                        ]
                    },
                }
            ],
        },
    )
    assert "信息条「张三」" in next(c for c in preview.changes if "文字层" in c["label"])["after"]
    layers = content(client, job_id, B)["overlays"]
    assert layers[0] == {
        "engine": "ass",
        "kind": "lower_third",
        "title": "张三",
        "subtitle": "研究员",
        "effect": "fade",
    }
    assert layers[1]["engine"] == "html" and "文字层：" in output.text
    with pytest.raises(ToolError, match="文字层无效"):
        run(
            client,
            config,
            "edit_shots",
            {
                "job_id": job_id,
                "operations": [{"op": "update", "shot": "#2", "fields": {"overlays": [{"kind": "title"}]}}],
            },
        )
    run(
        client,
        config,
        "edit_shots",
        {"job_id": job_id, "operations": [{"op": "update", "shot": "#2", "clear": ["overlays"]}]},
    )
    assert "overlays" not in content(client, job_id, B)


def test_merge_and_split_rules():
    fade, zoom = {"kind": "fade", "duration": 0.5}, {"kind": "zoom", "duration": 0.3}
    first = {"transition": {"enter": fade, "leave": fade}}
    merge_transition(first, {"transition": {"leave": zoom, "next": {"kind": "fade", "duration": 1}}})
    assert first["transition"] == {"enter": fade, "leave": zoom, "next": {"kind": "fade", "duration": 1}}
    plain = {}
    merge_transition(plain, {})
    assert "transition" not in plain
    original = {"enter": fade, "leave": zoom, "next": {"kind": "fade", "duration": 1}}
    assert split_transition(original, 0, 3) == {"enter": fade}
    assert split_transition(original, 1, 3) is None
    assert split_transition(original, 2, 3) == {"leave": zoom, "next": {"kind": "fade", "duration": 1}}
