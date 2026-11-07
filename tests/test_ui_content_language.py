"""Creation language is persistent; analysis previews follow the active UI language."""

import asyncio
import copy

import pytest
from fastapi.testclient import TestClient

from agent.llm.scripted import ScriptedLlm, say
from agent.llm.types import user_message
from api import create_app
from models.storyboard import QwenStoryboarder, query_instructions, split_instructions
from shared.analysis_text import select_analysis
from studio import studio
from studio import workspace as ws
from studio.material_digest import compose_digest


@pytest.mark.parametrize(
    "language,word,other", [("zh", "城市夜景", "City lights"), ("en", "City lights", "城市夜景")]
)
def test_digest_selects_language_before_truncating(language, word, other):
    segments = [
        {
            "id": "s1",
            "source_id": "source",
            "source_name": "Custom source",
            "kind": "shot",
            "start": 0,
            "end": 10,
            "source_duration": 10,
            "annotation_status": "complete",
            "views": {
                "analysis": (
                    "城市夜景，有人在路上行走。 / City lights and people walking on the street.\n"
                    "动作：行走 / walking\n场景：城市 / city\n对象：道路 / road"
                )
            },
        }
    ]
    original = copy.deepcopy(segments)
    digest = compose_digest(segments, scope=None, budget=2000, content_index_id="i", language=language)
    assert word in digest.text and other not in digest.text
    assert ("Storyline:" if language == "en" else "故事线：") in digest.text
    assert segments == original
    assert select_analysis("旧版中文", language) == "旧版中文"
    assert select_analysis("AC / DC", language) == "AC / DC"


@pytest.mark.parametrize("language,name", [("zh", "默认方案"), ("en", "Default Plan")])
def test_creation_language_persists_in_job_and_plan(config, language, name):
    with TestClient(create_app(config)) as client:
        response = client.post(
            "/api/studio/jobs", json={"text": "Test outline", "title": "Test", "language": language}
        )
        assert response.status_code == 201, response.text
        job = response.json()
        assert studio.load_job(config, job["id"])["language"] == language
        w = ws.load(config, job["id"])
        assert w["job_language"] == language
        assert w["plans"][w["active_plan"]]["name"] == name
        # User names and the creation language survive subsequent workspace loading.
        w["plans"][w["active_plan"]]["name"] = "Custom 中文 plan"
        ws.persist(config, job["id"], w)
        assert ws.load(config, job["id"])["plans"][w["active_plan"]]["name"] == "Custom 中文 plan"
        assert client.post("/api/studio/jobs", json={"text": "Test", "language": "fr"}).status_code == 422


def test_english_storyboard_prompts_and_query_rewrite(config, monkeypatch):
    prompt = split_instructions(10, language="en", vocabulary=True)
    assert "一句英文画面意象" in prompt and "query must be English only" in prompt
    assert "A misty mountain trail" in prompt
    assert "query 必须使用中文" not in prompt and '"visual": "晨雾' not in prompt
    assert "query must be English only" in query_instructions(language="en")
    assert "query 必须使用中文" in split_instructions(10)
    model = QwenStoryboarder(config)
    calls = []

    def chat(parts, instructions, feedback):
        calls.append(instructions)
        return '{"query":"old harbor lighthouse"}'

    monkeypatch.setattr(model, "_chat", chat)
    assert (
        model.suggest_query("A lighthouse.", "Old lighthouse", ("", ""), None, language="en")
        == "old harbor lighthouse"
    )
    assert "query must be English only" in calls[0]


def test_session_title_is_async_and_does_not_consume_conversation(config):
    async def scenario():
        main = ScriptedLlm([[say("Ready")]])
        gate = asyncio.Event()

        class GatedTitle(ScriptedLlm):
            async def stream(self, request):
                await gate.wait()
                async for chunk in super().stream(request):
                    yield chunk

        title = GatedTitle([[say("Harbor video planning")]])
        runtime = create_app(config, agent_llm=main).state.agent_runtime
        runtime.title_llm_factory = lambda: title
        sid = (await runtime.create_or_resume())["id"]
        await runtime.prompt(
            sid,
            [{"type": "text", "text": "Help plan a short video about the old harbor."}],
            context={"language": "en"},
        )
        await runtime.state(sid).task
        assert runtime.store.get(sid)["title"] == "Help plan a short video about the old harbor."[:30]
        assert not runtime.state(sid).title_task.done()
        gate.set()
        await runtime.state(sid).title_task
        assert runtime.store.get(sid)["title"] == "Harbor video planning"
        assert len(main.requests) == len(title.requests) == 1
        assert not title.requests[0].tools and "Use English" in title.requests[0].system
        events = runtime.store.events(sid)
        assert events[-1]["type"] == "plugin:mindex/session-title"
        assert not any(e["type"] == "assistant/message" and "Harbor video planning" in str(e) for e in events)
        assert runtime.status(sid) == "idle"
        # Explicit session names are never replaced by a generated title.
        custom = (await runtime.create_or_resume(title="My custom title"))["id"]
        await runtime.prompt(custom, user_message("hello")["content"])
        await runtime.state(custom).task
        assert runtime.state(custom).title_task is None

    asyncio.run(scenario())


def test_session_title_failure_preserves_fallback_and_cancel_stops_request(config):
    async def scenario():
        main = ScriptedLlm([[say("Ready")], [say("Ready")]])
        title = ScriptedLlm([{"blocks": [], "error": "offline"}, {"blocks": [say("Late")], "delay": 10}])
        runtime = create_app(config, agent_llm=main).state.agent_runtime
        runtime.title_llm_factory = lambda: title
        sid = (await runtime.create_or_resume())["id"]
        await runtime.prompt(sid, user_message("查询港口素材")["content"])
        await runtime.state(sid).task
        await runtime.state(sid).title_task
        assert runtime.store.get(sid)["title"] == "查询港口素材"
        other = (await runtime.create_or_resume())["id"]
        await runtime.prompt(other, user_message("another")["content"])
        await asyncio.sleep(0)
        await runtime.cancel(other)
        assert runtime.state(other).title_task.done()
        runtime.store.delete(other)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "language,visual,query",
    [
        ("en", "An old harbor lighthouse", "old harbor lighthouse"),
        ("zh", "港口的旧灯塔", "港口 旧灯塔"),
    ],
)
def test_split_execution_passes_creation_language_and_persists_results(
    config, monkeypatch, language, visual, query
):
    import json
    from types import SimpleNamespace

    from studio.workspace.handlers.split import SplitHandler

    job = studio.create_job(config, "A lighthouse by the harbor.", title="Test", language=language)
    w = ws.load(config, job["id"])
    p = w["plans"][w["active_plan"]]
    ws.add_script(config, job["id"], w, p, "A lighthouse by the harbor.", "manual")
    model = QwenStoryboarder(config)
    instructions_seen = []

    def chat(parts, instructions, feedback):
        instructions_seen.append(instructions)
        return json.dumps(
            {
                "shots": [
                    {
                        "text": "A lighthouse by the harbor.",
                        "visual": visual,
                        "query": query,
                        "estimated_seconds": 2,
                    }
                ]
            }
        )

    monkeypatch.setattr(model, "_chat", chat)
    ctx = SimpleNamespace(
        config=config,
        w=w,
        p=p,
        job_id=job["id"],
        version_id="split-test",
        services={"storyboarder": model},
        diagnostics=SimpleNamespace(record=lambda *args: None),
    )
    SplitHandler().run(ctx)
    ws.persist(config, job["id"], w)
    saved = ws.load(config, job["id"])
    saved_plan = saved["plans"][p["id"]]
    shot = next(iter(saved_plan["shots"].values()))
    content = saved["versions"][shot["content"]]["data"]
    assert content["visual"] == visual and content["query"] == query
    assert content["text"] == "A lighthouse by the harbor."
    assert ("query must be English only" if language == "en" else "query 必须使用中文") in instructions_seen[
        0
    ]
