"""选题挖掘的能力 × 入口对照表：每项能力都要同时提供 REST、Web 页面、CLI 与智能助手工具（MCP 同名导出）。

``tests/test_topics_parity.py`` 逐项断言：REST 路由已注册、CLI 命令存在、工具在注册表中且导出给 MCP、
页面文件含 ``data-capability="<page>"`` 的区块。新增能力时先在这里登记。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    name: str
    label: str
    rest: tuple[tuple[str, str], ...]  # (方法, 路径模板)
    cli: tuple[str, ...]  # "profiles list" 形式，相对 mindex topics
    tools: tuple[str, ...]
    page: str  # 页面区块 data-capability 值
    page_file: str = "studio/topics.html"  # 相对 frontend/src


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "profiles", "领域档案",
        rest=(("GET", "/api/topics/profiles"), ("POST", "/api/topics/profiles"),
              ("GET", "/api/topics/profiles/{profile_id}"), ("PATCH", "/api/topics/profiles/{profile_id}"),
              ("DELETE", "/api/topics/profiles/{profile_id}")),
        cli=("profiles list", "profiles show", "profiles create", "profiles update", "profiles delete"),
        tools=("list_topic_profiles", "save_topic_profile", "delete_topic_profile"),
        page="profiles",
    ),
    Capability(
        "sources", "数据源",
        rest=(("GET", "/api/topics/profiles/{profile_id}/sources"), ("POST", "/api/topics/profiles/{profile_id}/sources"),
              ("GET", "/api/topics/sources/{source_id}"), ("PATCH", "/api/topics/sources/{source_id}"),
              ("DELETE", "/api/topics/sources/{source_id}"), ("POST", "/api/topics/sources/{source_id}/test")),
        cli=("sources list", "sources add", "sources update", "sources remove", "sources test"),
        tools=("list_topic_sources", "save_topic_source", "delete_topic_source", "test_topic_source"),
        page="sources",
    ),
    Capability(
        "fetch", "立即抓取与候选条目",
        rest=(("POST", "/api/topics/profiles/{profile_id}/fetch"), ("GET", "/api/topics/profiles/{profile_id}/items")),
        cli=("fetch", "items"),
        tools=("fetch_topic_items", "list_topic_items"),
        page="fetch",
    ),
    Capability(
        "runs", "挖掘任务",
        rest=(("POST", "/api/topics/profiles/{profile_id}/runs"), ("GET", "/api/topics/profiles/{profile_id}/runs"),
              ("GET", "/api/topics/runs/{run_id}"), ("POST", "/api/topics/runs/{run_id}/cancel"),
              ("POST", "/api/topics/runs/{run_id}/retry")),
        cli=("discover", "runs list", "runs show", "runs cancel", "runs retry"),
        tools=("discover_topics", "get_topic_run", "manage_topic_run"),
        page="runs",
    ),
    Capability(
        "cards", "选题卡片",
        rest=(("GET", "/api/topics/profiles/{profile_id}/cards"), ("GET", "/api/topics/cards/{card_id}"),
              ("PATCH", "/api/topics/cards/{card_id}")),
        cli=("list", "show", "update"),
        tools=("list_topics", "get_topic", "update_topic"),
        page="cards",
    ),
    Capability(
        "score", "打分",
        rest=(("POST", "/api/topics/cards/{card_id}/score"),),
        cli=("score",), tools=("score_topic",), page="score",
    ),
    Capability(
        "coverage", "素材覆盖报告",
        rest=(("GET", "/api/topics/cards/{card_id}/coverage"),),
        cli=("coverage",), tools=("topic_coverage",), page="coverage",
    ),
    Capability(
        "outline", "大纲与推荐模板",
        rest=(("POST", "/api/topics/cards/{card_id}/outline"),),
        cli=("outline",), tools=("draft_topic_outline",), page="outline",
    ),
    Capability(
        "create-job", "从选题建创作任务",
        rest=(("POST", "/api/topics/cards/{card_id}/job"),),
        cli=("create-job",), tools=("create_job_from_topic",), page="create-job",
    ),
    Capability(
        "job-profile", "任务的账号档案快照与同步",
        rest=(("GET", "/api/topics/jobs/{job_id}/profile"), ("POST", "/api/topics/jobs/{job_id}/profile/sync")),
        cli=("job-profile show", "job-profile sync"), tools=("get_job_profile", "sync_job_profile"),
        page="job-profile", page_file="studio/studio.html",
    ),
    Capability(
        "gaps", "素材采集清单",
        rest=(("GET", "/api/topics/profiles/{profile_id}/gaps"), ("PATCH", "/api/topics/gaps/{gap_id}"),
              ("GET", "/api/topics/profiles/{profile_id}/gaps.csv")),
        cli=("gaps list", "gaps done", "gaps reopen", "gaps export"), tools=("topic_gaps", "update_topic_gap"),
        page="gaps",
    ),
    Capability(
        "calendar", "日历导入",
        rest=(("POST", "/api/topics/sources/{source_id}/calendar"), ("GET", "/api/topics/calendar-samples"),
              ("POST", "/api/topics/sources/{source_id}/calendar-sample/{name}")),
        cli=("sources import-calendar",), tools=("import_topic_calendar",), page="calendar",
    ),
    Capability(
        "schedule", "每日定时与启动补跑",
        rest=(("GET", "/api/topics/schedule"),),
        cli=("schedule status",), tools=("get_topic_schedule",), page="schedule",
    ),
)
