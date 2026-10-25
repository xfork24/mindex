"""选题挖掘 · 日历来源：ICS/CSV 解析、时间窗口与每年重复、导入（REST/CLI/智能助手）、样例打包与订阅地址。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from topics_fixtures import install
from typer.testing import CliRunner

from mindex import cli
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.topics import scoring
from mindex.topics.sources import calendar
from mindex.topics.sources.base import SourceError

ROOT = Path(__file__).resolve().parents[1]
TODAY = datetime.now().astimezone().date()


def ics(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def vevent(summary, start, extra=""):
    return f"BEGIN:VEVENT\r\nSUMMARY:{summary}\r\nDTSTART{start}\r\n{extra}END:VEVENT\r\n"


def test_parse_ics_and_csv():
    text = ics(vevent("苹果秋季\r\n 发布会", ";VALUE=DATE:20260909", "DESCRIPTION:新品\\, 发布\\n第二行\r\n"),
               vevent("财报", ":20261030T013000Z"),
               vevent("本地时间", ";TZID=Asia/Shanghai:20261101T090000"),
               vevent("周年", ":19690720", "RRULE:FREQ=YEARLY\r\n"),
               "BEGIN:VEVENT\r\nSUMMARY:没有日期\r\nEND:VEVENT\r\n")
    events = calendar.parse_ics(text)
    assert [e.title for e in events] == ["苹果秋季发布会", "财报", "本地时间", "周年"]
    assert events[0].date == "2026-09-09" and events[0].summary == "新品, 发布\n第二行"
    assert events[2].date == "2026-11-01" and events[3].recurring and not events[0].recurring
    with pytest.raises(SourceError, match="BEGIN:VCALENDAR"):
        calendar.parse_ics("hello")
    rows, errors = calendar.parse_csv("\ufeffdate,title,desc,kind,recurring\n10-24,程序员节,,节日,\n"
                                      "1969-07-20,登月,人类首次,历史,yearly\n2026-13-01,坏日期\n2026-11-01,\n")
    assert [(r.date, r.recurring, r.has_year) for r in rows] == [("2000-10-24", True, False), ("1969-07-20", True, True)]
    assert errors == ["第 4 行日期无效：2026-13-01", "第 5 行缺少标题"]
    with pytest.raises(SourceError, match="表头 date 与 title"):
        calendar.parse_csv("day,name\n1,2\n")
    assert calendar.parse_text(text)[0][0].title == "苹果秋季发布会"


def test_occurrences_window_and_anniversaries():
    today = date(2026, 10, 26)
    once = calendar.Event(date="2026-10-30", title="一次")
    assert calendar.occurrences(once, today, 14) == [date(2026, 10, 30)]
    assert calendar.occurrences(once, today, 3) == []
    assert calendar.occurrences(calendar.Event(date="2026-10-25", title="昨天"), today, 14) == []
    yearly = calendar.Event(date="1969-10-27", title="周年", recurring=True)
    assert calendar.occurrences(yearly, today, 14) == [date(2026, 10, 27)]
    new_year = calendar.Event(date="2000-01-02", title="跨年", recurring=True, has_year=False)
    assert calendar.occurrences(new_year, date(2026, 12, 25), 14) == [date(2027, 1, 2)]
    leap = calendar.Event(date="2000-02-29", title="闰日", recurring=True, has_year=False)
    assert calendar.occurrences(leap, date(2027, 2, 20), 14) == [date(2027, 2, 28)]
    future = calendar.Event(date="2030-10-27", title="还没开始", recurring=True)
    assert calendar.occurrences(future, today, 14) == []
    items = calendar.to_items([yearly, once], today, 14, "ts_1")
    assert [i.title for i in items] == ["周年（57 周年）", "一次"]
    assert items[0].event_at.startswith("2026-10-27T00:00:00") and items[0].extra["dedup"].startswith("cal:ts_1:2026-10-27")
    # 时效：当天事件 1，一周内线性升温
    now = datetime.combine(today, datetime.min.time()).astimezone()
    assert scoring.item_timeliness({"event_at": items[0].event_at}, now) == pytest.approx(1 - 1 / 7, abs=1e-3)


def test_samples_are_packaged_and_valid():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "samples/*.csv" in project["tool"]["setuptools"]["package-data"]["mindex.topics"]
    assert "*.csv" in (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    listed = {item["name"]: item["events"] for item in calendar.sample_list()}
    assert set(listed) == {"finance", "tech", "history"} and all(count >= 5 for count in listed.values())
    for name in listed:
        events, errors = calendar.parse_csv(calendar.sample_text(name))
        assert not errors and all(e.recurring for e in events)
    with pytest.raises(ValueError, match="没有这个日历样例"):
        calendar.sample_text("../secret")
    from mindex.desktop_check import run

    assert run(Path(tempfile.mkdtemp()))["topic_samples"] == 3


def soon(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def test_calendar_rest_import_and_fetch(config, monkeypatch):
    install(monkeypatch, {"https://cal.example.com/feed.ics": ics(vevent("订阅事件", f":{soon(2).replace('-', '')}"))
                          .encode()})
    with TestClient(create_app(config)) as client:
        pid = client.post("/api/topics/profiles", json={"name": "历史号", "domain": "历史"}).json()["id"]
        source = client.post(f"/api/topics/profiles/{pid}/sources",
                             json={"kind": "calendar", "name": "纪念日"}).json()
        sid = source["id"]
        assert source["events"] == 0
        assert "还没有导入日历事件" in client.post(f"/api/topics/sources/{sid}/test").json()["error"]
        text = f"date,title,desc\n{soon(1)},明天的事件,说明\n{soon(30)},太远的事件,\n"
        imported = client.post(f"/api/topics/sources/{sid}/calendar", json={"text": text}).json()
        assert imported["imported"] == 2 and imported["total"] == 2
        appended = client.post(f"/api/topics/sources/{sid}/calendar", params={"mode": "append"},
                               files={"file": ("events.ics", ics(vevent("文件事件", f":{soon(3).replace('-', '')}")),
                                               "text/calendar")}).json()
        assert appended["total"] == 3
        assert client.get(f"/api/topics/sources/{sid}").json()["events"] == 3
        tried = client.post(f"/api/topics/sources/{sid}/test").json()
        assert [i["title"] for i in tried["items"]] == ["明天的事件", "文件事件"]  # 窗口外的不取
        assert tried["items"][0]["event_at"].startswith(soon(1))
        assert client.post(f"/api/topics/profiles/{pid}/fetch").json()["new"] == 2
        assert client.post(f"/api/topics/profiles/{pid}/fetch").json()["new"] == 0  # 同一天的同一事件去重
        sample = client.post(f"/api/topics/sources/{sid}/calendar-sample/history").json()
        assert sample["sample"] == "history" and sample["total"] == sample["imported"] >= 10
        assert client.post(f"/api/topics/sources/{sid}/calendar-sample/nope").status_code == 422
        assert {i["name"] for i in client.get("/api/topics/calendar-samples").json()["items"]} == {"finance", "tech", "history"}
        bad = client.post(f"/api/topics/sources/{sid}/calendar", json={"text": "day,name\n1,2"})
        assert bad.status_code == 422 and "表头" in bad.json()["detail"]
        assert client.post(f"/api/topics/sources/{sid}/calendar", json={"text": "x", "mode": "bogus"}).status_code == 422
        config.topics.fetch.max_bytes = 1024
        huge = client.post(f"/api/topics/sources/{sid}/calendar", files={"file": ("big.csv", "date,title\n" + "x" * 3000)})
        assert huge.status_code == 413
        rss = client.post(f"/api/topics/profiles/{pid}/sources",
                          json={"kind": "rss", "name": "订阅", "config": {"url": "https://x.example.com/f"}}).json()
        wrong = client.post(f"/api/topics/sources/{rss['id']}/calendar", json={"text": text})
        assert wrong.status_code == 422 and "只有日历类型" in wrong.json()["detail"]
        # ICS 订阅地址：每次抓取时解析，不入事件表
        sub = client.post(f"/api/topics/profiles/{pid}/sources", json={
            "kind": "calendar", "name": "订阅日历", "config": {"url": "https://cal.example.com/feed.ics", "days": 7}}).json()
        assert client.post(f"/api/topics/sources/{sub['id']}/test").json()["items"][0]["title"] == "订阅事件"
        # 删除来源时一并删除事件
        client.delete(f"/api/topics/sources/{sid}")
        from mindex.shared.storage import Store

        assert Store(config.data_dir).one("SELECT COUNT(*) AS n FROM topic_calendar_events")["n"] == 0


def test_calendar_cli_and_agent(config, tmp_path):
    app = create_app(config)
    with TestClient(app) as client:
        pid = client.post("/api/topics/profiles", json={"name": "科技号", "domain": "科技"}).json()["id"]
        sid = client.post(f"/api/topics/profiles/{pid}/sources", json={"kind": "calendar", "name": "日程"}).json()["id"]
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(f"data_dir: {config.data_dir}\n", encoding="utf-8")
    runner = CliRunner()
    base = ["--config", str(yaml_path), "topics", "sources", "import-calendar", sid]
    events = tmp_path / "events.ics"
    events.write_text(ics(vevent("发布会", f":{soon(1).replace('-', '')}")), encoding="utf-8")
    assert json.loads(runner.invoke(cli.app, [*base, str(events)]).output)["total"] == 1
    assert json.loads(runner.invoke(cli.app, [*base, "--sample", "tech", "--append"]).output)["total"] > 1
    assert runner.invoke(cli.app, base).exit_code != 0  # 文件与样例二选一

    registry = build_registry()

    async def call(arguments):
        mindex = MindexClient(app)
        try:
            spec = registry.get("import_topic_calendar")
            args = spec.parse(arguments)
            ctx = ToolContext(client=mindex)
            preview = await spec.preview(args, ctx)
            return preview, await spec.execute(args, ctx)
        finally:
            await mindex.aclose()

    preview, out = asyncio.run(call({"topic_source_id": sid, "sample": "history"}))
    assert preview.changes[1]["after"] == "替换" and "已导入" in out.text and out.focus["section"] == "sources"
    _, out = asyncio.run(call({"topic_source_id": sid, "text": f"date,title\n{soon(2)},智能助手导入", "append": True}))
    assert out.value["imported"] == 1
    with pytest.raises(Exception, match="二选一"):
        registry.get("import_topic_calendar").parse({"topic_source_id": sid})
