"""风险分级与删除策略：智能助手工具不开放物理删除（Requirement 19）。

- 结构检查：write/danger 工具都有预览；设计文档风险表与注册表一致；只删记录的工具、GPU/发布/激活/构建类工具为
  danger；不存在删除素材、删除演员表导入图片的工具。
- 静态检查：工具源码中的 DELETE 调用只指向允许的记录删除接口。
- Property 14：用真实 ``MindexClient``（换成记录请求的 ``httpx.MockTransport``，不经网络、不碰文件）
执行全部工具的
  预览与执行；记录的是 httpx 规整后服务端实际会收到的 method + path。断言从不出现物理删除接口，
  ``DELETE /api/studio/jobs/{id}`` 只能由 ``delete_job`` 发出，且每个 DELETE 都是该工具白名单内的路径。
  参数取自参数 JSON Schema（最小参数集 + 随机可选字段 + 含 ``../``、``?``、``#`` 的恶意 ID），假服务的响应在
  成功与 404/409/422/500 之间随机。

假服务只提供足以让工具往下走的一组一致数据，并非完整复刻；工具因数据形状不全抛出的异常视为“工具已停止”，
性质只关心在停止之前从工具发出的请求。
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from agent.loopback import MindexClient, unsafe_path
from agent.registry import ToolContext, ToolError
from agent.tools import build_registry

REGISTRY = build_registry()
SPECS = REGISTRY.all()
TOOLS_DIR = Path(__file__).resolve().parents[1] / "backend" / "src" / "agent" / "tools"

# ------------------------------------------------------------------ 删除策略
# 物理删除接口（会删除磁盘文件）：任何工具都不得发出。
FORBIDDEN = (
    re.compile(r"^/api/admin/sources/[^/]+/?$"),
    re.compile(r"^/api/persons/imports/[^/]+/images/[^/]+/?$"),
)
STUDIO_JOB = re.compile(r"^/api/studio/jobs/[^/]+/?$")
# 允许发出 DELETE 的工具及其唯一允许的路径：delete_job 是用户确认保留的例外，其余只删除记录。
ALLOWED_DELETES = {
    "delete_group": re.compile(r"^/api/admin/groups/[^/]+$"),
    "delete_job": re.compile(r"^/api/studio/jobs/[^/]+$"),
    "delete_plan": re.compile(r"^/api/studio/jobs/[^/]+/plans/[^/]+$"),
    "remove_reference_text": re.compile(r"^/api/studio/jobs/[^/]+/plans/[^/]+/references/[^/]+$"),
    "delete_script_template": re.compile(r"^/api/studio/templates/[^/]+$"),
    "delete_brand": re.compile(r"^/api/studio/brands/[^/]+$"),
    "delete_person": re.compile(r"^/api/persons/entities/[^/]+$"),
    "remove_person_reference": re.compile(r"^/api/persons/references/[^/]+$"),
    "change_search_release": re.compile(r"^/api/persons/releases/[^/]+$"),
    "delete_model_profile": re.compile(r"^/api/settings/profiles/[^/]+$"),
    "reset_business_settings": re.compile(r"^/api/settings/business$"),
    "delete_voice": re.compile(r"^/api/settings/voices/[^/]+$"),
    "delete_topic_profile": re.compile(r"^/api/topics/profiles/[^/]+$"),
    "delete_topic_source": re.compile(r"^/api/topics/sources/[^/]+$"),
}

# 设计文档「风险分级汇总」中的新增工具（逐项核对）。
DESIGN_RISKS = {
    "read": "load_tools search_by_media find_similar sample_frames get_system_status "
    "list_index_versions check_index "
    "get_source list_segments get_segment_details get_source_report get_task_status diagnose_plan "
    "get_version_lineage suggest_shot_query get_material_digest preview_version get_selection_candidates "
    "preview_selection get_person identify_person list_review_shots list_search_releases list_voice_catalog",
    "write": "update_curation retry_task cancel_task upload_source upload_subtitle rename_job cancel_build "
    "attach_query_media confirm_selection save_person assign_source_work add_person_reference review_shot "
    "import_cast confirm_cast_import register_voice generate_lyrics",
    "danger": "activate_index start_source_task delete_plan run_column delete_person remove_person_reference "
    "publish_persons change_search_release delete_model_profile reset_business_settings delete_voice "
    "clone_voice start_local_service stop_local_service",
}
# GPU 任务、发布、激活、构建类工具（Req 19.4；含既有的构建与档案激活工具）。
HEAVY = (
    "start_source_task",
    "publish_persons",
    "activate_index",
    "change_search_release",
    "activate_model_profile",
    "run_build",
    "run_node",
    "run_column",
    "clone_voice",
    "start_local_service",
)


def check_requests(name: str, requests: list[tuple[str, str]]) -> None:
    """Property 14 的断言：记录的请求里没有物理删除，DELETE 只来自白名单工具的白名单路径。"""
    for method, path in requests:
        if method != "DELETE":
            continue
        for candidate in (path, unquote(path)):
            assert not any(p.match(candidate) for p in FORBIDDEN), f"{name} 发出了物理删除 {method} {path}"
            if STUDIO_JOB.match(candidate):
                assert name == "delete_job", f"{name} 发出了删除创作任务 {path}"
        allowed = ALLOWED_DELETES.get(name)
        assert allowed is not None and allowed.match(path), f"{name} 发出了未登记的 DELETE {path}"


# ------------------------------------------------------------------ 假服务数据（彼此一致）
JOB, PLAN_A, PLAN_B, SHOT = "job-0001", "plan-a", "plan-b", "shot-a1b2c3d4"
CONTENT_VERSION, BOARD_VERSION = "ver-content-1", "ver-board-1"
ATTACHMENTS = ("attachment://" + "a" * 32, "attachment://" + "b" * 32)
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def workspace() -> dict:
    plan = {
        "name": "方案",
        "created_at": "2024-01-01T00:00:00",
        "board": BOARD_VERSION,
        "script": None,
        "shots": {SHOT: {"content": CONTENT_VERSION}},
        "ranges": {},
        "options": {"references": [{"id": "c" * 64, "name": "参考文稿", "chars": 4}]},
    }
    return {
        "job": {"id": JOB, "title": "测试任务", "outline": "大纲"},
        "active_plan": PLAN_A,
        "revision": "rev-1",
        "conflicts": {},
        "builds": [],
        "plans": {
            PLAN_A: {**plan, "id": PLAN_A, "name": "方案甲"},
            PLAN_B: {**plan, "id": PLAN_B, "name": "方案乙", "created_at": "2024-02-01T00:00:00"},
        },
        "versions": {
            BOARD_VERSION: {
                "id": BOARD_VERSION,
                "stage": "board",
                "status": "complete",
                "data": {"order": [SHOT]},
            },
            CONTENT_VERSION: {
                "id": CONTENT_VERSION,
                "stage": "content",
                "status": "complete",
                "data": {
                    "id": SHOT,
                    "text": "旁白",
                    "query": "海边日落",
                    "estimated_seconds": 3.0,
                    "visual": None,
                },
            },
        },
    }


CATALOGUE = {
    "entities": [
        {"id": "actor-1", "kind": "actor", "name": "演员甲", "aliases": []},
        {
            "id": "role-1",
            "kind": "role",
            "name": "角色乙",
            "aliases": [],
            "work_ids": ["work-1"],
            "actor_ids": ["actor-1"],
        },
        {"id": "work-1", "kind": "work", "name": "作品丙", "aliases": []},
    ],
    "references": [
        {
            "id": "ref-1",
            "actor_id": "actor-1",
            "role_id": "role-1",
            "url": "/api/persons/references/ref-1/image",
        }
    ],
    "sources": [{"id": "src-1", "name": "素材一", "work_id": "work-1"}],
}
RELEASES = {
    "active": {"release_id": "rel-aaaaaa01", "content_id": "idx-aaaaaa01", "person_id": None},
    "releases": [
        {"id": "rel-aaaaaa01", "content_id": "idx-aaaaaa01", "person_id": None, "created_at": "2024-01-01"},
        {
            "id": "rel-bbbbbb02",
            "content_id": "idx-aaaaaa01",
            "person_id": "per-1",
            "created_at": "2024-02-01",
        },
    ],
}
INDEXES = {
    "active": {"content_id": "idx-aaaaaa01", "release_id": "rel-aaaaaa01"},
    "versions": [
        {"id": "idx-aaaaaa01", "status": "ready", "mode": "full", "format": 2, "created_at": "2024-01-01"},
        {"id": "idx-bbbbbb02", "status": "ready", "mode": "full", "format": 2, "created_at": "2024-02-01"},
    ],
}


def profile(pid: int, name: str) -> dict:
    return {
        "id": pid,
        "name": name,
        "provider": "openai",
        "config": {"model": "m", "base_url": "http://127.0.0.1"},
        "api_key": {"has_key": False},
    }


EFFECTIVE = {
    "backend": "openai",
    "base_url": "http://127.0.0.1",
    "model": "m",
    "voice": "alloy",
    "api_key": {"has_key": False},
}
SETTINGS = {
    "roles": [
        {
            "role": "tts",
            "label": "语音合成",
            "active_profile_id": 1,
            "effective": EFFECTIVE,
            "profiles": [profile(1, "在用"), profile(2, "备用")],
        },
        {
            "role": "agent",
            "label": "智能助手",
            "active_profile_id": 3,
            "effective": EFFECTIVE,
            "profiles": [profile(3, "助手")],
        },
    ],
    "business": {
        "enforce_gpu_lock": True,
        "default_voice": None,
        "tts_backend": "openai",
        "search": {"channel_weights": [1, 1, 1], "rrf_k": 60, "candidates": 100},
    },
    "voices": [
        {
            "id": 1,
            "name": "音色一",
            "voice_id": "v-1",
            "provider": "openai",
            "kind": "preset",
            "status": "ready",
        }
    ],
    "providers": {"openai": "OpenAI"},
    "presets": {},
}
SOURCE = {
    "id": "src-1",
    "name": "素材一",
    "prepared": True,
    "hidden": False,
    "favorite": False,
    "work": {},
    "media": {"duration": 60.0},
    "shot_count": 1,
}
BRAND = {
    "id": "user-brand0000001",
    "name": "我的品牌",
    "description": "",
    "version": 1,
    "builtin": False,
    "subtitle": None,
    "overlay": None,
    "intro": None,
    "outro": None,
    "watermark": None,
}
TEMPLATE = {
    "id": "user-0123456789ab",
    "name": "我的模板",
    "category": "base",
    "kind": "outline",
    "version": 1,
    "description": "说明",
    "instructions": "提示词",
    "constraints": {"max_chars": None},
    "builtin": False,
    "base_id": "base",
}

TOPIC_PROFILE = {
    "id": "tp_0123456789ab",
    "name": "财经号",
    "domain": "金融",
    "subtopics": [],
    "audience": "",
    "positioning": "",
    "taboos": [],
    "keywords": [],
    "language": "zh-hans",
    "region": "CN",
    "canvas": "16:9",
    "weights": {},
    "schedule": {"enabled": False, "time": "08:00", "count": 10},
}

TOPIC_SOURCE = {
    "id": "ts_0123456789ab",
    "profile_id": TOPIC_PROFILE["id"],
    "kind": "rss",
    "kind_label": "RSS",
    "name": "要闻",
    "enabled": True,
    "config": {"url": "https://example.com/feed.xml"},
    "has_secret": False,
    "secret_mask": None,
    "last_fetch_at": None,
    "last_error": None,
    "last_count": None,
}

TOPIC_RUN = {
    "id": "a" * 32,
    "profile_id": TOPIC_PROFILE["id"],
    "status": "failed",
    "trigger": "manual",
    "progress": {},
    "result": None,
    "error": "失败",
}
TOPIC_CARD = {
    "id": "tc_0123456789ab",
    "profile_id": TOPIC_PROFILE["id"],
    "run_id": "a" * 32,
    "title": "选题",
    "status": "new",
    "job_id": None,
    "total": 0.5,
    "angle": "",
    "reason": "",
    "visuals": ["a", "b"],
    "scores": {},
    "outline": None,
    "outline_origin": None,
    "template_id": "base",
    "warnings": [],
    "item_ids": [],
    "coverage": None,
    "items": [],
}

GET_ROUTES = [
    (re.compile(r"^/api/topics/profiles/[^/]+/gaps$"), lambda: {"items": []}),
    (
        re.compile(r"^/api/topics/jobs/[^/]+/profile$"),
        lambda: {
            "job_id": JOB,
            "topic": {"card_id": TOPIC_CARD["id"]},
            "hash": "h1",
            "current_hash": "h2",
            "outdated": True,
            "profile_exists": True,
            "snapshot": {},
        },
    ),
    (re.compile(r"^/api/topics/runs/[^/]+$"), lambda: TOPIC_RUN),
    (re.compile(r"^/api/topics/profiles/[^/]+/runs$"), lambda: {"items": [TOPIC_RUN]}),
    (re.compile(r"^/api/topics/profiles/[^/]+/cards$"), lambda: {"items": [TOPIC_CARD], "counts": {}}),
    (
        re.compile(r"^/api/topics/cards/[^/]+/coverage$"),
        lambda: {"card_id": TOPIC_CARD["id"], "title": "选题", "coverage": None},
    ),
    (re.compile(r"^/api/topics/cards/[^/]+$"), lambda: TOPIC_CARD),
    (re.compile(r"^/api/topics/profiles/[^/]+/sources$"), lambda: {"items": [TOPIC_SOURCE], "kinds": []}),
    (re.compile(r"^/api/topics/profiles/[^/]+/items$"), lambda: {"items": []}),
    (re.compile(r"^/api/topics/sources/[^/]+$"), lambda: TOPIC_SOURCE),
    (re.compile(r"^/api/topics/profiles/[^/]+$"), lambda: TOPIC_PROFILE),
    (re.compile(r"^/api/topics/profiles$"), lambda: {"items": [TOPIC_PROFILE]}),
    (re.compile(r"^/api/studio/templates/[^/]+$"), lambda: TEMPLATE),
    (re.compile(r"^/api/studio/templates$"), lambda: {"items": [TEMPLATE], "default": "base"}),
    (re.compile(r"^/api/studio/brands/[^/]+$"), lambda: BRAND),
    (re.compile(r"^/api/studio/brands$"), lambda: {"items": [BRAND]}),
    (re.compile(r"^/api/studio/jobs/[^/]+/workspace$"), workspace),
    (
        re.compile(r"^/api/studio/jobs/[^/]+$"),
        lambda: {"job": {"id": JOB, "title": "测试任务", "execution": {}}},
    ),
    (re.compile(r"^/api/studio/jobs$"), lambda: {"jobs": [{"id": JOB, "title": "测试任务"}]}),
    (re.compile(r"^/api/persons$"), lambda: CATALOGUE),
    (re.compile(r"^/api/persons/entities/[^/]+/linked-shots$"), lambda: {"total": 0, "items": []}),
    (re.compile(r"^/api/persons/releases$"), lambda: RELEASES),
    (re.compile(r"^/api/indexes$"), lambda: INDEXES),
    (
        re.compile(r"^/api/indexes/[^/]+/check$"),
        lambda: {"index_id": "idx-bbbbbb02", "ok": True, "issues": []},
    ),
    (re.compile(r"^/api/settings$"), lambda: SETTINGS),
    (re.compile(r"^/api/settings/local-services$"), lambda: LOCAL_SERVICES),
    (re.compile(r"^/api/admin/sources/[^/]+$"), lambda: SOURCE),
    (re.compile(r"^/api/admin/sources$"), lambda: {"items": [SOURCE], "total": 1}),
]
LOCAL_SERVICES = {
    "enabled": True,
    "items": [
        {
            "name": "wemm",
            "label": "WeMM 嵌入",
            "role": "embedding",
            "port": 8888,
            "running": False,
            "healthy": False,
            "external": False,
            "autostart": False,
            "stop_on_exit": True,
            "pid": None,
            "detail": "",
        }
    ],
}
FAULTS = (404, 409, 422, 500)


class FakeServer:
    """记录全部请求（httpx 规整后的 method + path）；按 ``faults`` 逐个请求注入错误，其余返回固定数据。"""

    def __init__(self, faults: list[int | None] | None = None):
        self.faults = list(faults or [])
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        index = len(self.requests) - 1
        fault = self.faults[index] if index < len(self.faults) else None
        if fault:
            return httpx.Response(fault, json={"detail": f"注入错误 {fault}"})
        if request.method == "GET":
            if path.startswith("/api/agent/attachments/") or "/frame" in path or path.endswith("/image"):
                return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
            for pattern, payload in GET_ROUTES:
                if pattern.match(path):
                    return httpx.Response(200, json=payload())
            return httpx.Response(200, json={})
        if request.method == "DELETE" and path.startswith("/api/studio/jobs/") and "/plans/" in path:
            return httpx.Response(200, json=workspace())
        return httpx.Response(200, json={"id": "new-1", "status": "queued"})


def resolve_attachment(ref: str):
    if ref not in ATTACHMENTS:
        raise ValueError("附件不存在或不属于当前调用方")
    return "sample.png", PNG, "image/png"


class FakeStore:
    def events(self, session_id, types=()):
        return []


class FakeRuntime:
    """面板提问通道：一律取消；按需加载只回显领域。"""

    registry = REGISTRY
    store = FakeStore()

    async def ask_question(self, session_id, call_id, questions):
        return {"cancelled": True}

    def load_domains(self, session_id, domains):
        return list(dict.fromkeys(domains))[-3:]


def context(client, panel: bool) -> ToolContext:
    gap = lambda *a: {"id": 1}
    if panel:
        return ToolContext(
            client=client,
            session_id="sess-1",
            agent_profile_id=3,
            page={"page": "studio", "job_id": JOB, "plan_id": PLAN_A},
            extra={"runtime": FakeRuntime(), "call_id": "call-1", "record_gap": gap},
        )
    return ToolContext(client=client, agent_profile_id=3, extra={"record_gap": gap})


async def _drive(spec, args, server: FakeServer, panel: bool) -> str:
    client = MindexClient(base_url="http://mindex.internal", resolve_attachment=resolve_attachment)
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(server), base_url="http://mindex.internal")
    ctx = context(client, panel)
    outcome = "ok"
    try:
        # 预览失败时运行时不会执行；这里照样执行（MCP 也不走预览），覆盖更多分支。
        if spec.preview is not None:
            try:
                await spec.preview(args, ctx)
            except Exception as exc:  # noqa: BLE001 — 假数据不完整导致的停止同样只看已发出的请求
                outcome = f"preview:{type(exc).__name__}"
        try:
            await spec.execute(args, ctx)
        except ToolError as exc:
            outcome = f"tool_error:{exc.code}"
        except Exception as exc:  # noqa: BLE001 — 记录崩溃类型供断言，不让单个工具中断扫描
            outcome = f"crash:{type(exc).__name__}"
    finally:
        await client.aclose()
    return outcome


def run_tool(
    name: str, arguments: dict, *, faults=None, panel: bool = True
) -> tuple[list[tuple[str, str]], str]:
    spec = REGISTRY.get(name)
    server = FakeServer(faults)
    try:
        args = spec.parse(arguments)
    except ToolError:
        return server.requests, "invalid_arguments"
    outcome = asyncio.run(_drive(spec, args, server, panel))
    return server.requests, outcome


# ------------------------------------------------------------------ 参数生成（JSON Schema → 值）
FIELD_VALUES = {
    "job_id": JOB,
    "plan_id": PLAN_B,
    "person_id": "actor-1",
    "reference_id": "ref-1",
    "release_id": "rel-bbbbbb02",
    "source_id": "src-1",
    "work_id": "work-1",
    "segment_id": "seg-1",
    "index_id": "idx-bbbbbb02",
    "task_id": "task-1",
    "import_id": "imp-1",
    "shot": "#1",
    "shot_id": SHOT,
    "version": "ver-content-1",
    "preview_token": "token-12345678",
    "actor_id": "actor-1",
    "role_id": "role-1",
    "build_id": "build-1",
    "candidate_id": "cand-1",
    "attachments": ATTACHMENTS[0],
    "profile_id": 2,
    "voice_id": 1,
}
ID_POOL = (
    JOB,
    PLAN_A,
    PLAN_B,
    SHOT,
    "#1",
    "actor-1",
    "role-1",
    "work-1",
    "ref-1",
    "src-1",
    "seg-1",
    "rel-aaaaaa01",
    "rel-bbbbbb02",
    "idx-bbbbbb02",
    "task-1",
    "imp-1",
    "img-1",
    "ver-content-1",
)
# 恶意 ID：路径规整（../）、编码点段、截断（? #）都试图把请求越到物理删除接口或删除创作任务。
HOSTILE = (
    "../../admin/sources/src-1",
    "../../../../admin/sources/src-1",
    "x/../../../../admin/sources/src-1",
    "../../persons/imports/imp-1/images/img-1",
    "../../../../persons/imports/imp-1/images/img-1",
    "%2E%2E/%2E%2E/admin/sources/src-1",
    "..%2F..%2Fadmin%2Fsources%2Fsrc-1",
    f"{JOB}?",
    f"{JOB}#",
    f"{JOB}?/plans/{PLAN_B}",
    "./src-1",
)


def fits(value: str, node: dict) -> bool:
    return node.get("minLength", 0) <= len(value) <= node.get("maxLength", 10**6)


def minimal(node: dict, name: str | None = None):
    """确定性的最小合法值：必填字段 + 每个字段按名称取假服务中存在的 ID。"""
    if "anyOf" in node:
        branch = next((b for b in node["anyOf"] if b.get("type") != "null"), node["anyOf"][0])
        return minimal(branch, name)
    if "enum" in node:
        return node["enum"][0]
    kind = node.get("type")
    if kind == "string":
        pattern = node.get("pattern")
        if pattern == "^attachment://":
            return ATTACHMENTS[0]
        if pattern:
            return "q1"
        value = FIELD_VALUES.get(name)
        if isinstance(value, str) and fits(value, node):
            return value
        return "测试" * max(1, (node.get("minLength", 1) + 1) // 2)
    if kind == "integer":
        value = FIELD_VALUES.get(name)
        return value if isinstance(value, int) else max(node.get("minimum", 1), 1)
    if kind == "number":
        low = node.get("minimum", node.get("exclusiveMinimum", 0))
        return float(low) + 1.0
    if kind == "boolean":
        return False
    if kind == "array":
        return [minimal(node.get("items", {}), name) for _ in range(node.get("minItems", 0))]
    if kind == "object":
        props = node.get("properties", {})
        return {key: minimal(props[key], key) for key in node.get("required", []) if key in props}
    return None


def strategy(node: dict, name: str | None = None) -> st.SearchStrategy:
    """随机合法（或接近合法）值：ID 池、恶意 ID、随机文本混合；可选字段随机出现。"""
    if "anyOf" in node:
        return st.one_of([strategy(branch, name) for branch in node["anyOf"]])
    if "enum" in node:
        return st.sampled_from(node["enum"])
    kind = node.get("type")
    if kind == "string":
        pattern = node.get("pattern")
        if pattern == "^attachment://":
            return st.sampled_from(ATTACHMENTS + ("attachment://" + "c" * 32, "attachment://../x"))
        if pattern:
            return st.from_regex(pattern, fullmatch=True)
        low, high = node.get("minLength", 0), min(node.get("maxLength", 40), 40)
        pool = [v for v in ID_POOL + HOSTILE if fits(v, node)]
        texts = st.text(
            st.characters(codec="utf-8", exclude_categories=("Cs", "Cc")),
            min_size=low,
            max_size=max(low, high),
        )
        named = FIELD_VALUES.get(name)
        options = [st.just(named)] if isinstance(named, str) and fits(named, node) else []
        return st.one_of(*options, st.sampled_from(pool) if pool else texts, texts)
    if kind == "integer":
        low, high = node.get("minimum", -3), node.get("maximum", 10**6)
        return st.one_of(
            st.sampled_from([v for v in (1, 2, 3) if low <= v <= high] or [low]),
            st.integers(min_value=low, max_value=high),
        )
    if kind == "number":
        low = node.get("minimum", node.get("exclusiveMinimum", -10.0))
        return st.floats(
            min_value=low,
            max_value=node.get("maximum", 10_000.0),
            allow_nan=False,
            exclude_min="exclusiveMinimum" in node,
        )
    if kind == "boolean":
        return st.booleans()
    if kind == "null":
        return st.none()
    if kind == "array":
        low = node.get("minItems", 0)
        high = min(node.get("maxItems", low + 3), low + 3)
        return st.lists(strategy(node.get("items", {}), name), min_size=low, max_size=high)
    if kind == "object":
        props = node.get("properties", {})
        required = set(node.get("required", []))
        return st.fixed_dictionaries(
            {k: strategy(v, k) for k, v in props.items() if k in required},
            optional={k: strategy(v, k) for k, v in props.items() if k not in required},
        )
    return st.none()


# 自动生成的最小参数集通不过模型校验（跨字段约束）或到不了关键分支时，补充手工示例。
MANUAL = {
    "update_plan_options": [{"name": "新方案名"}],
    "update_curation": [
        {"target": "source", "id": "src-1", "hidden": True},
        {"target": "segment", "id": "seg-1", "notes": "备注"},
    ],
    "search_media": [{"text": "海边日落"}, {"person_ids": ["actor-1"]}],
    "sample_frames": [{"source_id": "src-1"}, {"segment_id": "seg-1"}],
    "import_cast": [{"attachments": list(ATTACHMENTS)}],
    "save_person": [{"kind": "actor", "name": "新演员"}, {"person_id": "role-1", "name": "改名"}],
    "add_person_reference": [
        {"actor_id": "actor-1", "attachment": ATTACHMENTS[0]},
        {"role_id": "role-1", "source_id": "src-1", "timestamp": 1.5},
    ],
    "change_search_release": [
        {"release_id": "rel-bbbbbb02", "action": "delete"},
        {"release_id": "rel-aaaaaa01", "action": "delete"},
        {"release_id": "rel-bbbbbb02", "action": "activate"},
    ],
    "delete_person": [{"person_id": "actor-1"}, {"person_id": "role-1"}, {"person_id": "work-1"}],
    "delete_model_profile": [{"profile_id": 2}, {"profile_id": 1}, {"profile_id": 3}],
    "delete_plan": [{"job_id": JOB, "plan_id": PLAN_B}, {"plan_id": PLAN_A}],
    "clone_voice": [{"name": "新音色", "attachment": ATTACHMENTS[0]}],
    "add_reference_text": [{"text": "参考正文"}, {"attachment": ATTACHMENTS[0], "name": "附件文稿"}],
    "remove_reference_text": [{"reference": "参考文稿"}, {"reference": "c" * 8}],
    "delete_script_template": [{"template_id": TEMPLATE["id"]}],
    "update_script_template": [
        {"template_id": TEMPLATE["id"], "instructions": "新提示词"},
        {"template_id": TEMPLATE["id"], "max_chars": None},
    ],
    "copy_script_template": [{"template_id": "base", "name": "副本"}],
    "update_brand": [{"brand_id": BRAND["id"], "name": "改名"}, {"brand_id": BRAND["id"], "watermark": None}],
    "delete_brand": [{"brand_id": BRAND["id"]}],
    "copy_brand": [{"brand_id": "news", "name": "副本"}],
    "get_music_track": [{"track_id": "a" * 64}],
    "update_music_track": [
        {"track_id": "a" * 64, "tags": ["夜景"]},
        {"track_id": "a" * 64, "expires_at": None},
    ],
    "analyze_music_track": [{"track_id": "a" * 64}],
    "import_music_track": [
        {"attachment": ATTACHMENTS[0], "source": "自制", "license": "CC0", "attribution": "作者"}
    ],
    "recommend_music": [{"text": "夜晚的城市"}, {"shot": "#1"}],
    "test_tool_source": [{"source_id": "stub"}],
    "save_tool_source": [
        {
            "source_id": "stub",
            "name": "替身",
            "protocol": "http",
            "http": {"base_url": "https://tools.example.com"},
            "tools": [{"name": "make_video"}],
        }
    ],
    "save_stdio_tool_source": [
        {
            "source_id": "local",
            "name": "本机",
            "stdio": {"command": "/usr/bin/true"},
            "tools": [{"name": "make_video"}],
        }
    ],
    "start_local_service": [{"name": "wemm"}, {"name": "nope"}],
    "stop_local_service": [{"name": "wemm"}],
    "save_topic_profile": [
        {"name": "新档案", "domain": "金融"},
        {"topic_profile_id": TOPIC_PROFILE["id"], "taboos": ["不荐股"]},
    ],
    "delete_topic_profile": [{"topic_profile_id": TOPIC_PROFILE["id"]}],
    "save_topic_source": [
        {
            "topic_profile_id": TOPIC_PROFILE["id"],
            "kind": "rss",
            "name": "要闻",
            "config": {"url": "https://example.com/feed.xml"},
        },
        {"topic_source_id": TOPIC_SOURCE["id"], "enabled": False},
    ],
    "delete_topic_source": [{"topic_source_id": TOPIC_SOURCE["id"]}],
    "test_topic_source": [{"topic_source_id": TOPIC_SOURCE["id"]}],
    "fetch_topic_items": [{"topic_profile_id": TOPIC_PROFILE["id"]}],
    "list_topic_sources": [{"topic_profile_id": TOPIC_PROFILE["id"]}],
    "list_topic_items": [{"topic_profile_id": TOPIC_PROFILE["id"]}],
    "discover_topics": [{"topic_profile_id": TOPIC_PROFILE["id"]}],
    "get_topic_run": [{"run_id": TOPIC_RUN["id"]}, {"topic_profile_id": TOPIC_PROFILE["id"]}],
    "manage_topic_run": [
        {"run_id": TOPIC_RUN["id"], "action": "retry"},
        {"run_id": TOPIC_RUN["id"], "action": "cancel"},
    ],
    "list_topics": [{"topic_profile_id": TOPIC_PROFILE["id"]}],
    "get_topic": [{"card_id": TOPIC_CARD["id"]}],
    "score_topic": [{"card_id": TOPIC_CARD["id"]}],
    "topic_coverage": [{"card_id": TOPIC_CARD["id"]}],
    "draft_topic_outline": [{"card_id": TOPIC_CARD["id"]}],
    "update_topic": [{"card_id": TOPIC_CARD["id"], "status": "dismissed"}],
    "create_job_from_topic": [{"card_id": TOPIC_CARD["id"]}],
    "get_job_profile": [{"job_id": JOB}],
    "sync_job_profile": [{"job_id": JOB}],
    "topic_gaps": [{"topic_profile_id": TOPIC_PROFILE["id"]}],
    "import_topic_calendar": [
        {"topic_source_id": TOPIC_SOURCE["id"], "sample": "history"},
        {"topic_source_id": TOPIC_SOURCE["id"], "text": "date,title\n10-24,程序员节"},
    ],
    "update_topic_gap": [{"gap_id": "tg_0123456789ab", "status": "done"}],
    "edit_animation_source": [
        {"shot": "#1", "html": "<html><body></body></html>"},
        {"shot": "#1", "replacements": [{"find": "body", "replace": "main"}]},
    ],
}


def schema_of(spec) -> dict:
    return spec.parameters()


def examples(spec) -> list[dict]:
    """该工具的确定性参数集：最小参数集（能通过校验时）+ 手工示例。"""
    items = []
    base = minimal(schema_of(spec))
    try:
        spec.parse(base)
        items.append(base)
    except ToolError:
        pass
    return items + MANUAL.get(spec.name, [])


def hostile_variants(spec) -> list[dict]:
    """在每个合法参数集上，把每个自由字符串字段（无枚举、无 pattern）逐一替换为恶意 ID。"""
    props = schema_of(spec)["properties"]
    fields = [
        k
        for k, v in props.items()
        if any(
            b.get("type") == "string" and "enum" not in b and "pattern" not in b for b in v.get("anyOf", [v])
        )
    ]
    variants = []
    for base in examples(spec):
        for field in fields:
            variants += [{**base, field: value} for value in HOSTILE]
    return variants


def args_for(spec) -> st.SearchStrategy:
    options = [strategy(schema_of(spec))]
    fixed = examples(spec) + hostile_variants(spec)
    if fixed:
        options.append(st.sampled_from(fixed))
    return st.one_of(options)


# ------------------------------------------------------------------ 结构检查（Req 19.1–19.4）
def test_write_and_danger_tools_have_preview():
    missing = [s.name for s in SPECS if s.risk in ("write", "danger") and s.preview is None]
    assert missing == []
    # read 工具直接执行，不需要（也不应有）确认预览。
    assert [s.name for s in SPECS if s.risk == "read" and s.preview is not None] == []


def test_design_risk_table_matches_registry():
    for risk, names in DESIGN_RISKS.items():
        for name in names.split():
            spec = REGISTRY.get(name)
            assert spec is not None, f"注册表缺少设计中的工具 {name}"
            assert spec.risk == risk, f"{name} 应为 {risk}，实际 {spec.risk}"


def test_record_deletes_and_heavy_tools_are_danger():
    # 移除组成员是可逆关系编辑，经 POST 完成，不删除媒体或组记录。
    deleting = [
        s
        for s in SPECS
        if s.name.startswith(("delete_", "remove_", "reset_")) and s.name != "remove_group_members"
    ]
    assert {s.name for s in deleting} | {"change_search_release"} == set(ALLOWED_DELETES)
    for name in ALLOWED_DELETES:
        assert REGISTRY.get(name).risk == "danger", name
    for name in HEAVY:
        assert REGISTRY.get(name).risk == "danger", name


def test_no_physical_delete_tools_registered():
    names = {s.name for s in SPECS}
    assert not names & {
        "delete_source",
        "remove_source",
        "delete_import_image",
        "remove_import_image",
        "delete_cast_image",
    }
    for spec in SPECS:
        text = f"{spec.name} {spec.description}"
        assert not re.search(r"删除素材(?!报告)|删除演员表导入图片", text) or spec.name == "delete_job", (
            spec.name
        )


def test_tool_parameter_schemas_keep_title_fields():
    """回归：inline_schema 曾把名为 title 的字段当作 schema 注解删掉，rename_job 的必填参数对模型不可见。"""
    for spec in SPECS:
        schema = spec.parameters()
        assert set(schema.get("required", [])) <= set(schema["properties"]), spec.name
    assert "title" in REGISTRY.get("rename_job").parameters()["properties"]


# ------------------------------------------------------------------ 静态检查
def delete_templates(tree: ast.AST) -> list[str]:
    """源码中 ``*.delete(<路径>)`` 调用的路径模板（f-string 的插值替换为 X）。"""
    templates = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "delete"
        ):
            continue
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            templates.append(first.value)
        elif isinstance(first, ast.JoinedStr):
            templates.append("".join(p.value if isinstance(p, ast.Constant) else "X" for p in first.values))
        else:
            templates.append(f"<非字面量路径 line {node.lineno}>")
    return templates


def raw_deletes(tree: ast.AST) -> list[int]:
    """``request("DELETE", ...)`` 或 ``method="DELETE"`` 形式的调用（只允许经 client.delete 发 DELETE）。"""
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        values = [*node.args[:1], *(k.value for k in node.keywords if k.arg == "method")]
        if any(isinstance(v, ast.Constant) and str(v.value).upper() == "DELETE" for v in values):
            lines.append(node.lineno)
    return lines


def test_static_delete_calls_only_target_record_endpoints():
    found = []
    for file in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        assert raw_deletes(tree) == [], file.name
        for template in delete_templates(tree):
            found.append(template)
            assert not any(p.match(template) for p in FORBIDDEN), f"{file.name}: DELETE {template}"
            assert any(p.match(template) for p in ALLOWED_DELETES.values()), f"{file.name}: DELETE {template}"
    assert len(found) == len(ALLOWED_DELETES), found


# ------------------------------------------------------------------ 动态检查
def test_every_tool_has_valid_minimal_arguments():
    """每个工具至少有一组能通过参数校验的确定性参数（最小参数集或手工示例）。"""
    assert [s.name for s in SPECS if not examples(s)] == []


@pytest.mark.parametrize("name", sorted(ALLOWED_DELETES))
def test_record_delete_tools_reach_their_delete(name):
    """正向对照：记录删除工具在数据一致时确实发出 DELETE（证明记录器能看到删除请求）。"""
    arguments = {
        "change_search_release": {"release_id": "rel-bbbbbb02", "action": "delete"},
        "remove_reference_text": {"reference": "参考文稿"},
    }.get(name, examples(REGISTRY.get(name))[0])
    requests, outcome = run_tool(name, arguments)
    deletes = [path for method, path in requests if method == "DELETE"]
    assert deletes and all(ALLOWED_DELETES[name].match(p) for p in deletes), (outcome, requests)
    check_requests(name, requests)


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_ids_cannot_reach_physical_delete(value):
    """回归：delete_job / delete_plan 的 ID 参数经路径规整曾能打到删除素材或删除创作任务；
    现由回环客户端拒绝。
    """
    for name, arguments in (
        ("delete_job", {"job_id": value}),
        ("delete_plan", {"job_id": value, "plan_id": PLAN_B}),
        ("delete_plan", {"job_id": JOB, "plan_id": value}),
    ):
        for panel in (True, False):
            requests, outcome = run_tool(name, arguments, panel=panel)
            check_requests(name, requests)
            assert all(method != "DELETE" for method, _ in requests), (name, arguments, requests, outcome)


@pytest.mark.parametrize(
    "path",
    [
        "/api/studio/jobs/../../admin/sources/x",
        "/api/a/./b",
        "/api/a/%2e%2e/b",
        "/api/a/..%2Fb",
        "/api/studio/jobs/J?/plans/P",
        "/api/a#b",
        "api/a",
    ],
)
def test_unsafe_path_rejected(path):
    assert unsafe_path(path)


def test_unsafe_path_allows_normal_paths():
    assert unsafe_path("/api/studio/jobs/job-0001/plans/plan-b") is None
    assert unsafe_path("/api/admin/sources/src-1/frame?t=1.5&width=768", allow_query=True) is None
    assert unsafe_path("/api/admin/sources/src-1/frame?t=1.5") is not None


@pytest.mark.slow
def test_exhaustive_sweep_never_physically_deletes():
    """确定性全量扫描：每个工具 × 每组确定性参数（含恶意 ID 变体）× 面板/MCP 两种上下文。"""
    reached = set()
    for spec in SPECS:
        for arguments in examples(spec) + hostile_variants(spec):
            for panel in (True, False):
                requests, _ = run_tool(spec.name, arguments, panel=panel)
                check_requests(spec.name, requests)
                if requests:
                    reached.add(spec.name)
    # 不发请求的只能是纯运行时/界面工具（按需加载、提问、记录缺口、页面跳转），以及没有先 preview_selection
    # 时拒绝执行的 confirm_selection（选片预览存在进程内，确定性参数无法伪造）。
    assert {s.name for s in SPECS} - reached <= {
        "load_tools",
        "ask_user_question",
        "report_capability_gap",
        "navigate",
        "confirm_selection",
    }


# Feature: agent-capability-expansion, Property 14: 不开放物理删除
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    data=st.data(),
    spec=st.sampled_from(SPECS),
    panel=st.booleans(),
    faults=st.lists(st.one_of(st.none(), st.none(), st.sampled_from(FAULTS)), max_size=6),
)
def test_property_no_physical_delete(data, spec, panel, faults):
    """**Validates: Requirements 19.1, 19.2, 19.3, 19.4**"""
    arguments = data.draw(args_for(spec), label="arguments")
    requests, outcome = run_tool(spec.name, arguments, faults=faults, panel=panel)
    event(f"outcome={outcome.split(':')[0]}")
    check_requests(spec.name, requests)
    if outcome == "invalid_arguments":
        assert requests == []
    if spec.risk == "read":
        assert all(method in ("GET", "POST") for method, _ in requests), (spec.name, requests)
