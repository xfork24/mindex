"""`mindex studio` 命令组：在终端查看创作任务、预检、构建、跟踪与下载成片。

所有命令都经 REST 完成，与页面、智能助手和 MCP 走同一条路径（校验、乐观锁、studio 锁与 409 冲突原样生效）：
默认在本进程内装配应用并经 ASGI 调用（``agent.loopback.MindexClient``），``--url`` 时连接已运行的服务
（Web 或桌面版的 ``mindex serve``），令牌从 ``--token-env`` 指定的环境变量读取。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated, Any

import typer

from mindex.shared.cli_support import SortedGroup, print_json

app = typer.Typer(
    cls=SortedGroup,
    help="创作任务（studio）：jobs 列出任务，plans 查看方案，preflight 预检，build 构建（--watch 跟踪），"
    "status 查看构建日志，download 下载成片。\n\n"
    "默认在本进程内调用 REST；--url 连接已运行的 Mindex 服务（Web 或桌面版）。任务与方案 ID 可用唯一前缀，"
    "方案缺省为当前方案。--json 输出机器可读结果。详见 docs/cli-reference.md。\n\n"
    "示例：mindex studio jobs；mindex studio build JOB --watch；mindex studio download JOB -o film.mp4。",
    no_args_is_help=True,
)

TERMINAL = {"complete", "failed", "cancelled", "partial"}
FINAL_FILES = ("mp4", "srt", "vtt")


class StudioCliError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@app.callback()
def studio_options(
    ctx: typer.Context,
    url: Annotated[
        str | None, typer.Option("--url", help="连接已运行的 Mindex 服务，如 http://127.0.0.1:8000；省略时在本进程内调用。")
    ] = None,
    token_env: Annotated[
        str, typer.Option(help="连接远端服务时读取 Bearer 令牌的环境变量名。")
    ] = "MINDEX_WEB_TOKEN",
):
    ctx.meta["studio_url"] = url
    ctx.meta["studio_token_env"] = token_env


def _root_config(ctx: typer.Context):
    while ctx.parent is not None and ctx.obj is None:
        ctx = ctx.parent
    return ctx.obj


def make_app(config):
    """进程内模式装配的应用（测试替换为带确定性模型替身的应用）。"""
    from mindex.api import create_app

    return create_app(config)


class Session:
    """一条 CLI 命令内的 REST 会话：进程内 ASGI 或远端 HTTP。"""

    def __init__(self, ctx: typer.Context):
        from mindex.agent.loopback import MindexClient

        url = ctx.meta.get("studio_url")
        self.remote = bool(url)
        if url:
            token = os.environ.get(ctx.meta.get("studio_token_env") or "MINDEX_WEB_TOKEN", "").strip() or None
            self.client = MindexClient(base_url=url, token=token)
        else:
            config = _root_config(ctx)
            config.agent.mcp.http_enabled = False
            self.client = MindexClient(make_app(config))

    async def call(self, method: str, path: str, *, body: Any = None, **params) -> Any:
        result = await self.client.request(method, path, json=body, params=params or None)
        if not result["ok"]:
            raise StudioCliError(result["error"], result["status"])
        return result["data"]

    async def get(self, path: str, **params):
        return await self.call("GET", path, **params)

    async def post(self, path: str, body: Any = None):
        return await self.call("POST", path, body=body if body is not None else {})

    async def fetch(self, path: str) -> bytes:
        from mindex.agent.loopback import LoopbackError

        try:
            content, _ = await self.client.fetch_bytes(path)
        except LoopbackError as exc:
            raise StudioCliError(str(exc), exc.status) from exc
        return content

    async def aclose(self):
        await self.client.aclose()


def run(ctx: typer.Context, function):
    """执行一个异步命令体：统一错误提示与退出码（409 冲突 = 2，其余失败 = 1）。"""

    async def main():
        session = Session(ctx)
        try:
            return await function(session)
        finally:
            await session.aclose()

    try:
        return asyncio.run(main())
    except StudioCliError as exc:
        if exc.status == 409:
            typer.echo(f"冲突：{exc}（创作执行器可能正忙，或方案已在别处修改；稍后重试或重新预检）", err=True)
            raise typer.Exit(2) from exc
        typer.echo(f"失败：{exc}", err=True)
        raise typer.Exit(1) from exc


def unique(items: list[str], prefix: str, noun: str) -> str:
    if prefix in items:
        return prefix
    found = [item for item in items if item.startswith(prefix)]
    if len(found) == 1:
        return found[0]
    raise StudioCliError(f"{noun} {prefix} {'有多个匹配' if found else '不存在'}")


async def resolve_job(session: Session, job: str) -> str:
    items = (await session.get("/api/studio/jobs"))["items"]
    return unique([item["id"] for item in items], job, "任务")


async def load_workspace(session: Session, job: str, plan: str | None) -> tuple[str, dict, dict]:
    job_id = await resolve_job(session, job)
    w = await session.get(f"/api/studio/jobs/{job_id}/workspace", compact="true")
    if plan is None:
        return job_id, w, w["plans"][w["active_plan"]]
    names = {item["name"]: pid for pid, item in w["plans"].items()}
    pid = names.get(plan) or unique(list(w["plans"]), plan, "方案")
    return job_id, w, w["plans"][pid]


def node_counts(nodes: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for node in nodes:
        counts[node["action"]] = counts.get(node["action"], 0) + 1
    return counts


JsonOption = Annotated[bool, typer.Option("--json", help="输出 JSON。")]
PlanOption = Annotated[str | None, typer.Option("--plan", "-p", help="方案 ID（可用前缀）或名称；缺省为当前方案。")]
ForceOption = Annotated[bool, typer.Option("--force", help="忽略可复用版本全部重新生成（可能调用付费模型服务）。")]


@app.command("jobs")
def jobs(ctx: typer.Context, as_json: JsonOption = False):
    """列出创作任务（最新在前）。

    示例：mindex studio jobs --json
    """

    async def body(session):
        return (await session.get("/api/studio/jobs"))["items"]

    items = run(ctx, body)
    if as_json:
        print_json(items)
        return
    for item in items:
        typer.echo(f"{item['id']}\t{item['created_at']}\t{item['title']}")


@app.command("plans")
def plans(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
          as_json: JsonOption = False):
    """列出任务的方案：ID、名称、画幅、分镜数、是否有成片，* 标记当前方案。

    示例：mindex studio plans 3f2a
    """

    async def body(session):
        _, w, _ = await load_workspace(session, job, None)
        return [
            {"id": pid, "name": p["name"], "active": pid == w["active_plan"],
             "canvas": p["options"].get("canvas", "16:9"), "shots": len(p.get("shots") or {}),
             "final": p.get("final")}
            for pid, p in w["plans"].items()
        ]

    rows = run(ctx, body)
    if as_json:
        print_json(rows)
        return
    for row in rows:
        mark = "*" if row["active"] else " "
        typer.echo(f"{mark} {row['id']}\t{row['name']}\t{row['canvas']}\t{row['shots']} 镜\t"
                   f"{'有成片' if row['final'] else '无成片'}")


@app.command("preflight")
def preflight(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
              plan: PlanOption = None, force: ForceOption = False, as_json: JsonOption = False):
    """构建预检：列出将复用、重建或被阻塞的节点，不执行。

    示例：mindex studio preflight 3f2a --plan 竖屏版
    """

    async def body(session):
        job_id, _, p = await load_workspace(session, job, plan)
        return await session.get(f"/api/studio/jobs/{job_id}/plans/{p['id']}/preflight", force=str(force).lower())

    data = run(ctx, body)
    if as_json:
        print_json(data)
        return
    print_nodes(data["nodes"])


def print_nodes(nodes):
    counts = node_counts(nodes)
    typer.echo("，".join(f"{key} {value}" for key, value in sorted(counts.items())) or "没有节点")
    for node in nodes:
        if node["action"] != "reuse":
            reason = f"：{node['reason']}" if node.get("reason") else ""
            typer.echo(f"  {node['action']}\t{node['key']}{reason}")


async def watch_build(session: Session, job_id: str, build_id: str, *, quiet: bool, interval: float = 1.0) -> dict:
    after = 0
    while True:
        data = await session.get(f"/api/studio/jobs/{job_id}/builds/{build_id}/logs", after=after)
        for event in data["items"]:
            if not quiet:
                typer.echo(f"[{event.get('level', 'info')}] {event['message']}", err=True)
        after = data["next_seq"]
        if data["status"] in TERMINAL and not data["has_more"]:
            return data
        if not data["has_more"]:
            await asyncio.sleep(interval)


@app.command("build")
def build(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
          plan: PlanOption = None, force: ForceOption = False,
          watch: Annotated[bool, typer.Option("--watch", "-w", help="跟踪日志直到构建结束；失败或部分完成时退出码为 1。")] = False,
          all_canvases: Annotated[bool, typer.Option("--all-canvases", help="同时构建多画幅重新渲染导出的关联子方案。")] = False,
          as_json: JsonOption = False):
    """预检后提交整体构建。同一时间只能有一个构建，冲突时退出码为 2。

    进程内模式（未给 --url）下构建线程属于本进程，命令会一直等到构建结束；连接运行中的服务时提交后立即返回，
    加 --watch 跟踪。

    示例：

    \b
      mindex studio build 3f2a --watch
      mindex studio --url http://127.0.0.1:8000 build 3f2a
    """

    async def body(session):
        wait = watch or not session.remote
        job_id, _, p = await load_workspace(session, job, plan)
        extra = {"all_canvases": "true"} if all_canvases else {}
        checked = await session.get(f"/api/studio/jobs/{job_id}/plans/{p['id']}/preflight", force=str(force).lower(),
                                    **extra)
        submitted = await session.post(f"/api/studio/jobs/{job_id}/plans/{p['id']}/build",
                                       {"token": checked["token"], "force": force,
                                        **({"all_canvases": True} if all_canvases else {})})
        result = {"job_id": job_id, "plan_id": p["id"], "build_id": submitted["id"],
                  "nodes": len(submitted["nodes"]), "counts": node_counts(checked["nodes"])}
        if wait:
            if not as_json:
                typer.echo(f"已提交构建 {submitted['id']}（{len(submitted['nodes'])} 个节点）", err=True)
            result["status"] = (await watch_build(session, job_id, submitted["id"], quiet=as_json))["status"]
        return result

    result = run(ctx, body)
    if as_json:
        print_json(result)
    elif "status" in result:
        typer.echo(f"构建 {result['build_id']}：{result['status']}")
    else:
        typer.echo(f"已提交构建 {result['build_id']}（{result['nodes']} 个节点）；"
                   f"查看进度：mindex studio --url … status {result['job_id']} {result['build_id']}")
    if result.get("status") not in (None, "complete"):
        raise typer.Exit(1)


@app.command("status")
def status(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
           build_id: Annotated[str | None, typer.Argument(help="构建 ID；缺省为该任务最近一次构建。")] = None,
           watch: Annotated[bool, typer.Option("--watch", "-w", help="跟踪直到构建结束。")] = False,
           as_json: JsonOption = False):
    """查看构建状态与执行日志。

    示例：mindex studio status 3f2a；mindex studio status 3f2a 9c1d --watch
    """

    async def body(session):
        job_id = await resolve_job(session, job)
        bid = build_id
        if bid is None:
            w = await session.get(f"/api/studio/jobs/{job_id}/workspace", compact="true")
            if not w["builds"]:
                raise StudioCliError("该任务还没有构建")
            bid = w["builds"][-1]["id"]
        if watch:
            data = await watch_build(session, job_id, bid, quiet=as_json)
            return {"job_id": job_id, "build_id": bid, "status": data["status"], "items": []}
        data = await session.get(f"/api/studio/jobs/{job_id}/builds/{bid}/logs")
        return {"job_id": job_id, **data}

    data = run(ctx, body)
    if as_json:
        print_json(data)
        return
    for event in data.get("items", []):
        typer.echo(f"[{event.get('level', 'info')}] {event['message']}")
    typer.echo(f"构建 {data['build_id']}：{data['status']}")


@app.command("download")
def download(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
             plan: PlanOption = None,
             file: Annotated[str, typer.Option("--file", "-f", help="成片文件：mp4 / srt / vtt。")] = "mp4",
             export: Annotated[str | None, typer.Option("--export", help="下载某个画幅的导出成片（如 9:16）而不是主成片。")] = None,
             output: Annotated[Path | None, typer.Option("--output", "-o", help="保存路径；缺省为当前目录的 <任务>-<方案>.<扩展名>。")] = None,
             as_json: JsonOption = False):
    """下载方案当前选用的成片文件（mp4、srt 或 vtt），写入前校验 sha256。

    示例：mindex studio download 3f2a -o film.mp4
    """
    if file not in FINAL_FILES:
        raise typer.BadParameter(f"只能是 {' / '.join(FINAL_FILES)}", param_hint="--file")

    async def body(session):
        import hashlib
        from urllib.parse import quote

        job_id, w, p = await load_workspace(session, job, plan)
        vid = (p.get("finals") or {}).get(f"export:{export}") if export else p.get("final")
        final = w["versions"].get(vid or "")
        if not final or final["status"] != "complete":
            raise StudioCliError(f"该方案还没有 {export} 画幅的导出成片" if export else "该方案还没有成片，请先构建")
        if file not in final["files"]:
            raise StudioCliError(f"这个成片没有 {file} 文件")
        meta = final["files"][file]
        content = await session.fetch(f"/api/studio/jobs/{job_id}/artifacts/{quote(meta['path'], safe='/')}")
        if hashlib.sha256(content).hexdigest() != meta["sha256"]:
            raise StudioCliError("下载内容与成片版本记录的 sha256 不一致")
        suffix = f"-{export.replace(':', 'x')}" if export else ""
        target = output or Path(f"{job_id}-{p['id']}{suffix}.{file}")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(content)
        partial.replace(target)
        return {"job_id": job_id, "plan_id": p["id"], "version": final["id"], "path": str(target),
                "bytes": len(content), "sha256": meta["sha256"]}

    result = run(ctx, body)
    if as_json:
        print_json(result)
    else:
        typer.echo(f"已保存 {result['path']}（{result['bytes']} 字节）")


# ---------------------------------------------------------------- 分镜后期参数

shot_app = typer.Typer(
    cls=SortedGroup,
    help="分镜后期参数：show 查看分镜，set 设置转场等后期字段（经方案 PATCH 保存，乐观锁冲突时退出码为 2）。\n\n"
    "示例：mindex studio shot set JOB '#2' --enter fade:0.4 --next dissolve:0.5。",
    no_args_is_help=True,
)
app.add_typer(shot_app, name="shot")

EDGE_KINDS = ("fade", "white", "zoom", "slide")
OVERLAP_KINDS = ("fade", "dissolve", "fadeblack", "fadewhite", "wipeleft", "wiperight", "slideleft", "slideright",
                 "circleopen", "circleclose", "smoothleft", "smoothright")


def parse_edge(value: str, kinds: tuple[str, ...], option: str) -> dict:
    """``KIND[:秒]`` → ``{"kind", "duration"}``（时长缺省 0.5）。"""
    kind, _, seconds = value.partition(":")
    if kind not in kinds:
        raise typer.BadParameter(f"可选：{' / '.join(kinds)}", param_hint=option)
    try:
        duration = float(seconds) if seconds else 0.5
    except ValueError as exc:
        raise typer.BadParameter("时长必须是秒数，例如 fade:0.5", param_hint=option) from exc
    return {"kind": kind, "duration": duration}


def parse_overlays(title, lower_third, card, raw, engine) -> list[dict]:
    """文字层快捷参数与 JSON → Shot.overlays 的元素（经 ShotOverlay 校验）。"""
    import json

    from pydantic import ValidationError

    from mindex.studio.studio import ShotOverlay

    if engine not in ("ass", "html"):
        raise typer.BadParameter("只能是 ass 或 html", param_hint="--engine")
    found = []
    if title:
        head, _, tail = title.partition("|")
        found.append({"engine": engine, "kind": "title", "title": head, "subtitle": tail or None})
    if lower_third:
        head, _, tail = lower_third.partition("|")
        found.append({"engine": engine, "kind": "lower_third", "title": head, "subtitle": tail or None})
    if card:
        head, _, rows = card.partition("|") if "|" in card else ("", "", card)
        items = []
        for row in filter(None, (part.strip() for part in rows.split(","))):
            label, sep, value = row.partition("=")
            if not sep:
                raise typer.BadParameter("数据行写成 标签=值", param_hint="--card")
            items.append({"label": label.strip(), "value": value.strip()})
        found.append({"engine": engine, "kind": "data_card", "title": head or None, "items": items})
    for text in raw or []:
        try:
            found.append(json.loads(text))
        except ValueError as exc:
            raise typer.BadParameter(f"不是合法 JSON：{exc}", param_hint="--overlay") from exc
    try:
        return [ShotOverlay.model_validate(item).model_dump() for item in found]
    except ValidationError as exc:
        message = exc.errors()[0]["msg"].removeprefix("Value error, ")
        raise typer.BadParameter(message, param_hint="--title/--lower-third/--card/--overlay") from exc


def resolve_shot_ref(order: list[str], ref: str) -> str:
    if ref.startswith("#") and ref[1:].isdigit():
        index = int(ref[1:])
        if not 1 <= index <= len(order):
            raise StudioCliError(f"分镜序号 {ref} 超出范围（共 {len(order)} 个）")
        return order[index - 1]
    return unique(order, ref, "分镜")


async def board_shots(session: Session, job: str, plan: str | None):
    job_id, w, p = await load_workspace(session, job, plan)
    if not p.get("board"):
        raise StudioCliError("该方案还没有分镜")
    order = w["versions"][p["board"]]["data"]["order"]
    shots = [{"id": sid, **w["versions"][p["shots"][sid]["content"]]["data"]} for sid in order]
    for item in shots:
        item.pop("shot_id", None)
    return job_id, w, p, order, shots


ShotArgument = Annotated[str, typer.Argument(help="分镜：完整 ID、唯一前缀或 #序号（从 1 开始）。")]


@shot_app.command("show")
def shot_show(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
              shot: ShotArgument, plan: PlanOption = None, as_json: JsonOption = False):
    """查看分镜内容（旁白、查询、画面来源、背景音乐、转场等）。

    示例：mindex studio shot show 3f2a '#1' --json
    """

    async def body(session):
        _, _, _, order, shots = await board_shots(session, job, plan)
        sid = resolve_shot_ref(order, shot)
        return next(item for item in shots if item["id"] == sid)

    data = run(ctx, body)
    if as_json:
        print_json(data)
        return
    from mindex.studio.workspace.transitions import describe

    typer.echo(f"{data['id']}：{data['text']}")
    typer.echo(f"  查询：{data['query']}")
    typer.echo(f"  转场：{describe(data.get('transition'))}")
    typer.echo(f"  字幕高亮词：{'、'.join(data.get('highlights') or []) or '无'}")
    from mindex.agent.tools.studio_write import overlays_text

    typer.echo(f"  文字层：{overlays_text(data.get('overlays'))}")


@shot_app.command("set")
def shot_set(
    ctx: typer.Context,
    job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
    shot: ShotArgument,
    plan: PlanOption = None,
    enter: Annotated[str | None, typer.Option("--enter", help="入场镜内转场 KIND[:秒]，KIND 为 fade/white/zoom/slide。")] = None,
    leave: Annotated[str | None, typer.Option("--leave", help="出场镜内转场 KIND[:秒]（设置了 --next 时被取代）。")] = None,
    next_: Annotated[str | None, typer.Option("--next", help="到下一镜的重叠转场 KIND[:秒]，KIND 为 xfade 样式（fade、"
                                                         "dissolve、wipeleft、slideleft、circleopen 等）。")] = None,
    clear_transition: Annotated[bool, typer.Option("--clear-transition", help="恢复硬切（先清除再应用本次给出的转场）。")] = False,
    highlight: Annotated[list[str] | None, typer.Option("--highlight", help="字幕高亮词，可重复；整体替换该分镜的高亮词"
                                                                       "（烧录字幕时变色）。")] = None,
    clear_highlights: Annotated[bool, typer.Option("--clear-highlights", help="清空字幕高亮词。")] = False,
    title: Annotated[str | None, typer.Option("--title", help="追加标题卡：'标题' 或 '标题|副标题'。")] = None,
    lower_third: Annotated[str | None, typer.Option("--lower-third", help="追加下三分之一信息条：'姓名|职位'。")] = None,
    card: Annotated[str | None, typer.Option("--card", help="追加数据卡片：'标题|标签=值,标签=值'。")] = None,
    overlay: Annotated[list[str] | None, typer.Option("--overlay", help="追加文字层 JSON（与智能助手 overlays 的元素一致），"
                                                                    "可重复。")] = None,
    engine: Annotated[str, typer.Option("--engine", help="--title/--lower-third/--card 的方式：ass 烧录或 html 叠加。")] = "ass",
    clear_overlays: Annotated[bool, typer.Option("--clear-overlays", help="先去掉该分镜已有的文字层。")] = False,
    tool: Annotated[str | None, typer.Option("--tool", help="把画面来源设为第三方工具：工具源ID/工具名（tools list 列出）。")] = None,
    tool_args: Annotated[str | None, typer.Option("--tool-args", help="--tool 的 JSON 参数；字符串里的 {{text}}/{{visual}}/"
                                                                  "{{query}} 替换为本镜内容。")] = None,
    tool_output: Annotated[str, typer.Option("--tool-output", help="--tool 的产物类型：video 或 image（须与登记一致）。")] = "video",
    as_json: JsonOption = False,
):
    """修改一个分镜的后期字段并保存；未给出的字段保持不变。只有该分镜的渲染与成片在下次构建时重建。

    示例：

    \b
      mindex studio shot set 3f2a '#1' --enter fade:0.4
      mindex studio shot set 3f2a '#2' --next dissolve:0.6
      mindex studio shot set 3f2a '#2' --clear-transition
      mindex studio shot set 3f2a '#1' --highlight 人工智能 --highlight 芯片
      mindex studio shot set 3f2a '#1' --clear-overlays --title '第一章|起源'
      mindex studio shot set 3f2a '#3' --lower-third '张三|研究员' --card '营收|Q1=12亿,Q2=15亿'
      mindex studio shot set 3f2a '#4' --tool gen/make_video --tool-args '{"prompt": "{{visual}}"}'
    """
    edges = {}
    if enter:
        edges["enter"] = parse_edge(enter, EDGE_KINDS, "--enter")
    if leave:
        edges["leave"] = parse_edge(leave, EDGE_KINDS, "--leave")
    if next_:
        edges["next"] = parse_edge(next_, OVERLAP_KINDS, "--next")
    layers = parse_overlays(title, lower_third, card, overlay, engine)
    tool_source = None
    if tool is not None:
        import json

        tool_id, sep, tool_name = tool.partition("/")
        if not sep or not tool_id or not tool_name:
            raise typer.BadParameter("格式为 工具源ID/工具名", param_hint="--tool")
        if tool_output not in ("video", "image"):
            raise typer.BadParameter("只能是 video 或 image", param_hint="--tool-output")
        try:
            values = json.loads(tool_args) if tool_args else {}
        except ValueError as exc:
            raise typer.BadParameter(f"不是合法 JSON：{exc}", param_hint="--tool-args") from exc
        if not isinstance(values, dict):
            raise typer.BadParameter("须为 JSON 对象", param_hint="--tool-args")
        tool_source = {"kind": "tool", "tool_id": tool_id, "tool": tool_name, "arguments": values, "output": tool_output}
    if (not edges and not clear_transition and highlight is None and not clear_highlights and not layers
            and not clear_overlays and tool_source is None):
        raise typer.BadParameter("至少给出一个要修改的字段", param_hint="--enter/--leave/--next/--highlight/--clear-*")
    words = None
    if highlight is not None:
        from mindex.studio.studio import normalize_highlights

        try:
            words = normalize_highlights(highlight)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--highlight") from exc

    async def body(session):
        job_id, w, p, order, shots = await board_shots(session, job, plan)
        sid = resolve_shot_ref(order, shot)
        target = next(item for item in shots if item["id"] == sid)
        if edges or clear_transition:
            transition = {} if clear_transition else dict(target.get("transition") or {})
            transition.update(edges)
            if transition:
                target["transition"] = transition
            else:
                target.pop("transition", None)
        if clear_highlights:
            target.pop("highlights", None)
        if words:
            target["highlights"] = words
        if tool_source is not None:
            target["source"] = tool_source
        if clear_overlays or layers:
            merged = [*([] if clear_overlays else target.get("overlays") or []), *layers]
            if len(merged) > 4:
                raise StudioCliError("每个分镜最多 4 个文字层，请先 --clear-overlays")
            if merged:
                target["overlays"] = merged
            else:
                target.pop("overlays", None)
        from mindex.studio.workspace import revision as plan_revision

        revision = w["revision"] if w["active_plan"] == p["id"] else plan_revision(p)
        saved = await session.call("PATCH", f"/api/studio/jobs/{job_id}/plans/{p['id']}",
                                   body={"revision": revision, "shots": shots})
        fresh = saved["plans"][p["id"]]
        data = saved["versions"][fresh["shots"][sid]["content"]]["data"]
        return {"job_id": job_id, "plan_id": p["id"], "shot_id": sid, "transition": data.get("transition"),
                "highlights": data.get("highlights"), "overlays": data.get("overlays"), "source": data.get("source")}

    result = run(ctx, body)
    if as_json:
        print_json(result)
        return
    from mindex.studio.workspace.transitions import describe

    words = "、".join(result["highlights"] or []) or "无"
    from mindex.agent.tools.studio_write import overlays_text

    typer.echo(f"已保存分镜 {result['shot_id']}：转场 {describe(result['transition'])}；字幕高亮词 {words}；"
               f"文字层 {overlays_text(result['overlays'])}")


# ---------------------------------------------------------------- 方案后期参数

plan_app = typer.Typer(
    cls=SortedGroup,
    help="方案后期参数：show 查看方案参数，set 修改字幕烧录与字幕样式等（经方案 PATCH 保存）。\n\n"
    "示例：mindex studio plan set JOB --burn --subtitle-style highlight。",
    no_args_is_help=True,
)
app.add_typer(plan_app, name="plan")
SUBTITLE_STYLES = ("default", "classic", "outline", "box", "highlight")


@plan_app.command("show")
def plan_show(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
              plan: PlanOption = None, as_json: JsonOption = False):
    """查看方案参数（画幅、字幕烧录、字幕样式等）。

    示例：mindex studio plan show 3f2a --json
    """

    async def body(session):
        job_id, _, p = await load_workspace(session, job, plan)
        return {"job_id": job_id, "plan_id": p["id"], "name": p["name"], "options": p["options"]}

    data = run(ctx, body)
    if as_json:
        print_json(data)
        return
    typer.echo(f"{data['plan_id']}\t{data['name']}")
    for key, value in data["options"].items():
        typer.echo(f"  {key}: {value}")


@plan_app.command("set")
def plan_set(
    ctx: typer.Context,
    job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
    plan: PlanOption = None,
    burn: Annotated[bool | None, typer.Option("--burn/--soft", help="烧录字幕 / 软字幕轨。")] = None,
    subtitle_style: Annotated[str | None, typer.Option(
        "--subtitle-style", help="烧录字幕的样式：classic、outline、box、highlight；default 恢复配置的字体字号。")] = None,
    brand: Annotated[str | None, typer.Option("--brand", help="品牌模板 ID（brand list 列出），none 不使用品牌。")] = None,
    disable: Annotated[list[str] | None, typer.Option("--disable", help="关闭所选品牌模板中的 intro / outro / watermark，"
                                                                    "可重复；给出时整体替换已关闭的项。")] = None,
    clear_overrides: Annotated[bool, typer.Option("--clear-overrides", help="清除方案对品牌模板的全部覆盖。")] = False,
    intro: Annotated[str | None, typer.Option("--intro", help="本方案替换片头：本地视频路径、<sha256><后缀>，或 none 取消替换。")] = None,
    outro: Annotated[str | None, typer.Option("--outro", help="本方案替换片尾：同 --intro。")] = None,
    beat_align: Annotated[float | None, typer.Option(
        "--beat-align", min=0, max=1, help="节拍对齐容差（秒，0.04–1）：把背景音乐段落内的切点推迟到该范围内最近的节拍；"
                                           "0 关闭。")] = None,
    as_json: JsonOption = False,
):
    """修改方案后期参数并保存；未给出的参数保持不变。字幕样式只在烧录字幕时生效，变化后只重新合成成片。

    示例：

    \b
      mindex studio plan set 3f2a --burn --subtitle-style box
      mindex studio plan set 3f2a --subtitle-style default
      mindex studio plan set 3f2a --brand user-0123 --disable watermark
      mindex studio plan set 3f2a --outro special-outro.mp4
      mindex studio plan set 3f2a --beat-align 0.3
    """
    updates: dict = {}
    if burn is not None:
        updates["burn"] = burn
    if subtitle_style is not None:
        if subtitle_style not in SUBTITLE_STYLES:
            raise typer.BadParameter(f"可选：{' / '.join(SUBTITLE_STYLES)}", param_hint="--subtitle-style")
        updates["subtitle_style"] = None if subtitle_style == "default" else subtitle_style
    if brand is not None:
        updates["brand_id"] = None if brand == "none" else brand
    if beat_align is not None:
        updates["beat_align"] = {"tolerance": beat_align} if beat_align else None
    if disable is not None and set(disable) - {"intro", "outro", "watermark"}:
        raise typer.BadParameter("只能是 intro、outro 或 watermark", param_hint="--disable")
    if not updates and disable is None and not clear_overrides and intro is None and outro is None:
        raise typer.BadParameter("至少给出一个要修改的参数", param_hint="--burn/--soft/--subtitle-style/--brand")

    async def body(session):
        from mindex.studio.workspace import revision as plan_revision

        job_id, w, p = await load_workspace(session, job, plan)
        options = {key: value for key, value in p["options"].items() if key != "references"}
        overrides = None if clear_overrides else dict(options.get("brand_overrides") or {})
        if disable is not None:
            overrides = {**(overrides or {}), "disable": list(dict.fromkeys(disable))}
        for key, value in (("intro", intro), ("outro", outro)):
            if value is not None:
                overrides = {**(overrides or {}), key: None if value == "none" else {
                    "asset": await asset_value(session, value)}}
        touched = disable is not None or clear_overrides or intro is not None or outro is not None
        updates_all = {**updates, **({"brand_overrides": overrides or None} if touched else {})}
        revision = w["revision"] if w["active_plan"] == p["id"] else plan_revision(p)
        saved = await session.call("PATCH", f"/api/studio/jobs/{job_id}/plans/{p['id']}",
                                   body={"revision": revision, "options": {**options, **updates_all}})
        return {"job_id": job_id, "plan_id": p["id"], "options": saved["plans"][p["id"]]["options"]}

    result = run(ctx, body)
    if as_json:
        print_json(result)
        return
    options = result["options"]
    typer.echo(f"已保存方案 {result['plan_id']}：{'烧录' if options['burn'] else '软'}字幕，"
               f"字幕样式 {options.get('subtitle_style') or 'default'}，品牌 {options.get('brand_id') or '无'}，"
               f"节拍对齐 {('容差 ' + format(options['beat_align']['tolerance'], 'g') + ' 秒') if options.get('beat_align') else '关闭'}")


# ---------------------------------------------------------------- 品牌模板

brand_app = typer.Typer(
    cls=SortedGroup,
    help="品牌模板：list 列出，show 查看，copy 复制为自己的模板，update 修改（字幕样式、文字层样式、片头片尾、水印），"
    "delete 删除，upload 上传品牌素材。方案用 mindex studio plan set --brand 选用。\n\n"
    "示例：mindex studio brand copy news 我的频道；mindex studio brand update user-xxxx --watermark logo.png。",
    no_args_is_help=True,
)
app.add_typer(brand_app, name="brand")
BrandArgument = Annotated[str, typer.Argument(help="品牌模板 ID（brand list 列出）。")]


def brand_line(item: dict) -> str:
    marks = "，".join(f"{label}{'有' if item.get(key) else '无'}" for key, label in
                     (("intro", "片头"), ("outro", "片尾"), ("watermark", "水印")))
    return f"{item['id']}\t{item['name']}\t{'内置' if item['builtin'] else '用户'}\t{marks}"


@brand_app.command("list")
def brand_list(ctx: typer.Context, as_json: JsonOption = False):
    """列出品牌模板（内置在前）。

    示例：mindex studio brand list
    """

    async def body(session):
        return (await session.get("/api/studio/brands"))["items"]

    items = run(ctx, body)
    if as_json:
        print_json(items)
        return
    for item in items:
        typer.echo(brand_line(item))


@brand_app.command("show")
def brand_show(ctx: typer.Context, brand: BrandArgument, as_json: JsonOption = False):
    """查看品牌模板的字幕样式、文字层样式、片头片尾与水印。

    示例：mindex studio brand show news --json
    """

    async def body(session):
        return await session.get(f"/api/studio/brands/{brand}")

    item = run(ctx, body)
    if as_json:
        print_json(item)
        return
    from mindex.agent.tools.studio_brands import brand_text

    typer.echo(brand_text(item))


@brand_app.command("copy")
def brand_copy(ctx: typer.Context, brand: BrandArgument,
               name: Annotated[str, typer.Argument(help="新模板名称（≤40 字）。")], as_json: JsonOption = False):
    """把内置或用户品牌模板复制为新的用户模板。

    示例：mindex studio brand copy news 我的频道
    """

    async def body(session):
        return await session.post(f"/api/studio/brands/{brand}/copy", {"name": name})

    item = run(ctx, body)
    if as_json:
        print_json(item)
    else:
        typer.echo(f"已复制为 {item['id']}「{item['name']}」")


async def upload_asset(session: Session, path: Path) -> dict:
    """上传一个本地文件为品牌素材，返回 asset 引用。"""
    import asyncio
    import mimetypes

    def read() -> bytes | None:
        return path.read_bytes() if path.is_file() else None

    content = await asyncio.to_thread(read)
    if content is None:
        raise StudioCliError(f"找不到文件：{path}")
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    response = await session.client.http.post("/api/studio/brand-assets",
                                              files={"file": (path.name, content, kind)})
    from mindex.agent.loopback import _normalize

    result = _normalize(response)
    if not result["ok"]:
        raise StudioCliError(result["error"], result["status"])
    return {key: result["data"][key] for key in ("sha256", "suffix", "kind", "name")}


async def asset_value(session: Session, value: str) -> dict:
    """``<sha256><后缀>`` 形式的已上传素材，或本地文件路径（自动上传）。"""
    import re

    found = re.fullmatch(r"([0-9a-f]{64})(\.[a-z0-9]{2,5})", value)
    if found:
        kind = "image" if found.group(2) in (".png", ".jpg", ".jpeg", ".webp") else "video"
        return {"sha256": found.group(1), "suffix": found.group(2), "kind": kind}
    return await upload_asset(session, Path(value))


@brand_app.command("upload")
def brand_upload(ctx: typer.Context, path: Annotated[Path, typer.Argument(help="图片（水印）或视频（片头片尾，≤30 秒）。")],
                 as_json: JsonOption = False):
    """上传品牌素材，输出可在 update 中引用的 ``<sha256><后缀>``。

    示例：mindex studio brand upload logo.png
    """

    async def body(session):
        return await upload_asset(session, path)

    asset = run(ctx, body)
    if as_json:
        print_json(asset)
    else:
        typer.echo(f"{asset['sha256']}{asset['suffix']}\t{asset['kind']}")


@brand_app.command("update")
def brand_update(
    ctx: typer.Context,
    brand: BrandArgument,
    name: Annotated[str | None, typer.Option("--name", help="新名称。")] = None,
    description: Annotated[str | None, typer.Option("--description", help="新说明。")] = None,
    subtitle: Annotated[str | None, typer.Option("--subtitle", help="字幕样式：预设名（classic/outline/box/highlight）、"
                                                                "JSON 对象，或 none 去掉。")] = None,
    overlay: Annotated[str | None, typer.Option("--overlay", help="文字层样式 JSON（font、primary、accent、background、"
                                                              "background_opacity），或 none 去掉。")] = None,
    intro: Annotated[str | None, typer.Option("--intro", help="片头：本地视频路径（自动上传）、已上传的 <sha256><后缀>，"
                                                          "或 none 去掉。")] = None,
    outro: Annotated[str | None, typer.Option("--outro", help="片尾：同 --intro。")] = None,
    watermark: Annotated[str | None, typer.Option("--watermark", help="水印图片：本地路径、<sha256><后缀> 或 none。")] = None,
    position: Annotated[str, typer.Option("--watermark-position", help="水印位置：top-left/top-right/bottom-left/"
                                                                      "bottom-right。")] = "top-right",
    opacity: Annotated[float, typer.Option("--watermark-opacity", help="水印不透明度 0.05–1。")] = 0.8,
    scale: Annotated[float, typer.Option("--watermark-scale", help="水印宽度占画面短边的比例 0.02–0.5。")] = 0.12,
    as_json: JsonOption = False,
):
    """修改用户品牌模板；未给出的字段保持不变。使用它的方案下次构建只重新合成成片。

    示例：

    \b
      mindex studio brand update user-0123 --subtitle box --watermark logo.png --watermark-position bottom-right
      mindex studio brand update user-0123 --intro intro.mp4 --outro none
    """
    import json

    from mindex.studio.post_styles import SUBTITLE_PRESETS

    def parse(value, option):
        if value == "none":
            return None
        if option == "--subtitle" and value in SUBTITLE_PRESETS:
            from mindex.studio.workspace.ass import preset

            return preset(value)
        try:
            return json.loads(value)
        except ValueError as exc:
            raise typer.BadParameter("需要 JSON 对象或 none", param_hint=option) from exc

    changes: dict = {}
    if name is not None:
        changes["name"] = name
    if description is not None:
        changes["description"] = description
    if subtitle is not None:
        changes["subtitle"] = parse(subtitle, "--subtitle")
    if overlay is not None:
        changes["overlay"] = parse(overlay, "--overlay")
    if not changes and intro is None and outro is None and watermark is None:
        raise typer.BadParameter("至少修改一项", param_hint="--name/--subtitle/--intro/--watermark …")

    async def body(session):
        for key, value in (("intro", intro), ("outro", outro)):
            if value is not None:
                changes[key] = None if value == "none" else {"asset": await asset_value(session, value)}
        if watermark is not None:
            changes["watermark"] = None if watermark == "none" else {
                "asset": await asset_value(session, watermark), "position": position, "opacity": opacity,
                "scale": scale}
        return await session.call("PUT", f"/api/studio/brands/{brand}", body=changes)

    item = run(ctx, body)
    if as_json:
        print_json(item)
        return
    from mindex.agent.tools.studio_brands import brand_text

    typer.echo(brand_text(item))


@brand_app.command("delete")
def brand_delete(ctx: typer.Context, brand: BrandArgument,
                 yes: Annotated[bool, typer.Option("--yes", "-y", help="不再确认。")] = False):
    """删除用户品牌模板（只删记录；素材文件与已合成成片保留）。被方案引用时拒绝（退出码 2）。

    示例：mindex studio brand delete user-0123 --yes
    """
    if not yes and not typer.confirm(f"删除品牌模板 {brand}？"):
        raise typer.Exit(1)

    async def body(session):
        return await session.call("DELETE", f"/api/studio/brands/{brand}")

    run(ctx, body)
    typer.echo(f"已删除品牌模板 {brand}")


# ---------------------------------------------------------------- HTML 叠加层源码

overlay_app = typer.Typer(
    cls=SortedGroup,
    help="HTML 文字层的叠加层源码：show 输出某一层源码，edit 用本地文件整份替换（人工版本被钉住，构建时保留）。\n\n"
    "示例：mindex studio overlay show JOB '#2' --index 0 > layer.html；mindex studio overlay edit JOB '#2' layer.html。",
    no_args_is_help=True,
)
app.add_typer(overlay_app, name="overlay")
IndexOption = Annotated[int, typer.Option("--index", "-i", help="文字层在分镜 overlays 中的序号（从 0 开始）。")]


async def overlay_target(session: Session, job: str, plan: str | None, shot: str, index: int):
    job_id, w, p, order, shots = await board_shots(session, job, plan)
    sid = resolve_shot_ref(order, shot)
    layers = next(item for item in shots if item["id"] == sid).get("overlays") or []
    if index >= len(layers) or layers[index].get("engine") != "html":
        raise StudioCliError(f"分镜 {sid} 的第 {index + 1} 个文字层不是 HTML 文字层")
    return job_id, w, p, sid


@overlay_app.command("show")
def overlay_show(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
                 shot: ShotArgument, index: IndexOption = 0, plan: PlanOption = None):
    """输出当前选用叠加层版本中某一层的 HTML 源码（标准输出）。

    示例：mindex studio overlay show 3f2a '#2' --index 0 > layer.html
    """

    async def body(session):
        job_id, w, p, sid = await overlay_target(session, job, plan, shot, index)
        vid = p["shots"][sid].get("overlay")
        if not vid or f"overlay-{index}" not in w["versions"][vid].get("files", {}):
            raise StudioCliError("这一层还没有叠加层源码，请先构建或执行生成叠加层")
        return (await session.fetch(f"/api/studio/jobs/{job_id}/versions/{vid}/overlay-{index}.html")).decode("utf-8")

    typer.echo(run(ctx, body), nl=False)


@overlay_app.command("edit")
def overlay_edit(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
                 shot: ShotArgument, source: Annotated[Path, typer.Argument(help="新的 HTML 源码文件（背景须透明）。")],
                 index: IndexOption = 0, plan: PlanOption = None, as_json: JsonOption = False):
    """用本地文件整份替换某一层叠加层源码（先做安全检查）；只重建该分镜的渲染与成片。

    示例：mindex studio overlay edit 3f2a '#2' layer.html --index 0
    """
    if not source.is_file():
        raise typer.BadParameter(f"找不到文件：{source}", param_hint="SOURCE")
    html = source.read_text(encoding="utf-8")

    async def body(session):
        from mindex.studio.workspace import revision as plan_revision

        job_id, w, p, sid = await overlay_target(session, job, plan, shot, index)
        revision = w["revision"] if w["active_plan"] == p["id"] else plan_revision(p)
        saved = await session.call("PATCH", f"/api/studio/jobs/{job_id}/plans/{p['id']}/shots/{sid}/overlay",
                                   body={"revision": revision, "index": index, "html": html})
        return {"job_id": job_id, "plan_id": p["id"], "shot_id": sid,
                "version": saved["plans"][p["id"]]["shots"][sid].get("overlay")}

    result = run(ctx, body)
    if as_json:
        print_json(result)
    else:
        typer.echo(f"已保存人工叠加层源码（版本 {result['version']}）")


# ---------------------------------------------------------------- 多画幅导出

export_app = typer.Typer(
    cls=SortedGroup,
    help="多画幅导出：list 查看，set 设置某个画幅（derive 快速派生 / rerender 重新渲染），clear 取消。"
    "rerender 自动维护关联子方案，用 mindex studio build --all-canvases 一起构建；download --export 下载导出成片。\n\n"
    "示例：mindex studio export set JOB 9:16 --mode derive --fit crop。",
    no_args_is_help=True,
)
app.add_typer(export_app, name="export")
CANVASES = ("16:9", "9:16", "1:1")


async def save_exports(session: Session, job: str, plan: str | None, change) -> dict:
    from mindex.studio.workspace import revision as plan_revision

    job_id, w, p = await load_workspace(session, job, plan)
    if (p.get("variant_of") or {}).get("plan"):
        raise StudioCliError("这是多画幅重新渲染的子方案；请在主方案上设置导出")
    exports = change([dict(item) for item in p["options"].get("exports") or []])
    options = {key: value for key, value in p["options"].items() if key != "references"}
    revision = w["revision"] if w["active_plan"] == p["id"] else plan_revision(p)
    saved = await session.call("PATCH", f"/api/studio/jobs/{job_id}/plans/{p['id']}",
                               body={"revision": revision, "options": {**options, "exports": exports or None}})
    plan_data = saved["plans"][p["id"]]
    children = [item["id"] for item in saved["plans"].values() if (item.get("variant_of") or {}).get("plan") == p["id"]]
    return {"job_id": job_id, "plan_id": p["id"], "exports": plan_data["options"].get("exports"), "children": children}


def print_exports(result, as_json):
    if as_json:
        print_json(result)
        return
    for item in result["exports"] or []:
        typer.echo(f"{item['canvas']}\t{item['mode']}\t{item['fit']}")
    if not result["exports"]:
        typer.echo("不导出其他画幅")
    if result.get("children"):
        typer.echo(f"关联子方案：{'、'.join(result['children'])}（mindex studio build --all-canvases 一起构建）")


@export_app.command("list")
def export_list(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
                plan: PlanOption = None, as_json: JsonOption = False):
    """查看方案的多画幅导出设置与已导出的成片。

    示例：mindex studio export list 3f2a
    """

    async def body(session):
        job_id, _, p = await load_workspace(session, job, plan)
        made = {key.split(":", 1)[1]: vid for key, vid in (p.get("finals") or {}).items() if key.startswith("export:")}
        return {"job_id": job_id, "plan_id": p["id"], "exports": p["options"].get("exports"), "built": made}

    print_exports(run(ctx, body), as_json)


@export_app.command("set")
def export_set(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
               canvas: Annotated[str, typer.Argument(help="导出画幅：16:9 / 9:16 / 1:1（不能与方案画幅相同）。")],
               mode: Annotated[str, typer.Option("--mode", help="derive 快速派生 / rerender 重新渲染。")] = "derive",
               fit: Annotated[str, typer.Option("--fit", help="crop 裁切铺满 / pad 加边完整保留。")] = "crop",
               plan: PlanOption = None, as_json: JsonOption = False):
    """设置（或替换）某个画幅的导出。

    示例：mindex studio export set 3f2a 9:16 --mode rerender
    """
    if canvas not in CANVASES:
        raise typer.BadParameter(f"可选：{' / '.join(CANVASES)}", param_hint="CANVAS")
    if mode not in ("derive", "rerender") or fit not in ("crop", "pad"):
        raise typer.BadParameter("--mode 为 derive/rerender，--fit 为 crop/pad", param_hint="--mode/--fit")

    async def body(session):
        def change(items):
            return [*[item for item in items if item["canvas"] != canvas], {"canvas": canvas, "mode": mode, "fit": fit}]

        return await save_exports(session, job, plan, change)

    print_exports(run(ctx, body), as_json)


@export_app.command("clear")
def export_clear(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
                 canvas: Annotated[str | None, typer.Argument(help="要取消的画幅；省略时全部取消。")] = None,
                 plan: PlanOption = None, as_json: JsonOption = False):
    """取消某个（或全部）画幅的导出；不再需要的重新渲染子方案保留并解除关联。

    示例：mindex studio export clear 3f2a 9:16
    """

    async def body(session):
        return await save_exports(session, job, plan,
                                  lambda items: [item for item in items if canvas and item["canvas"] != canvas])

    print_exports(run(ctx, body), as_json)


# ---------------------------------------------------------------- 平台发布

PLATFORMS = ("bilibili", "douyin", "youtube")


@app.command("platforms")
def platforms_command(ctx: typer.Context, as_json: JsonOption = False):
    """列出平台发布预设：画幅、封面尺寸与标题/简介/标签/章节限制。

    示例：mindex studio platforms
    """

    async def body(session):
        return (await session.get("/api/studio/platforms"))["items"]

    items = run(ctx, body)
    if as_json:
        print_json(items)
        return
    for item in items:
        typer.echo(f"{item['id']}\t{item['name']}\t{item['canvas']}\t封面 {item['cover']['width']}×{item['cover']['height']}"
                   f"\t标题≤{item['title']['max_chars']}\t标签≤{item['tags']['max_count']}")


publish_app = typer.Typer(
    cls=SortedGroup,
    help="平台发布：set 设置平台与封面，show 查看设置与产物，build 构建发布包，download 下载某个平台的发布包 zip；"
    "metadata 查看与修改发布元数据，cover 查看封面候选帧。\n\n"
    "示例：mindex studio publish set JOB --platform bilibili --platform douyin；mindex studio publish build JOB --watch。",
    no_args_is_help=True,
)
app.add_typer(publish_app, name="publish")
PlatformArgument = Annotated[str, typer.Argument(help="平台：bilibili / douyin / youtube。")]


def check_platform(value):
    if value not in PLATFORMS:
        raise typer.BadParameter(f"可选：{' / '.join(PLATFORMS)}", param_hint="PLATFORM")


@publish_app.command("set")
def publish_set(
    ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
    platform: Annotated[list[str] | None, typer.Option("--platform", help="发布平台，可重复；整体替换。")] = None,
    clear: Annotated[bool, typer.Option("--clear", help="不再发布。")] = False,
    cover: Annotated[str | None, typer.Option("--cover", help="封面来源：frame / aigc / upload。")] = None,
    time_: Annotated[float | None, typer.Option("--cover-time", help="frame：成片中的秒数（省略自动挑最清晰的镜头）。")] = None,
    prompt: Annotated[str | None, typer.Option("--cover-prompt", help="aigc：封面提示词。")] = None,
    image: Annotated[Path | None, typer.Option("--cover-image", help="upload：本地图片（自动上传）。")] = None,
    title: Annotated[str | None, typer.Option("--cover-title", help="封面标题（省略用发布元数据标题）。")] = None,
    subtitle: Annotated[str | None, typer.Option("--cover-subtitle", help="封面副标题。")] = None,
    plan: PlanOption = None, as_json: JsonOption = False,
):
    """设置方案的发布平台与封面；未给出的封面字段保持不变。

    示例：

    \b
      mindex studio publish set 3f2a --platform bilibili --platform youtube --cover-subtitle 第 1 期
      mindex studio publish set 3f2a --cover upload --cover-image cover.png
    """
    for item in platform or []:
        check_platform(item)
    if cover is not None and cover not in ("frame", "aigc", "upload"):
        raise typer.BadParameter("可选：frame / aigc / upload", param_hint="--cover")

    async def body(session):
        from mindex.studio.workspace import revision as plan_revision

        job_id, w, p = await load_workspace(session, job, plan)
        current = p["options"].get("publish") or {}
        value = None
        if not clear:
            chosen = list(dict.fromkeys(platform)) if platform else current.get("platforms")
            if not chosen:
                raise StudioCliError("请用 --platform 选择至少一个平台")
            options = dict(current.get("cover") or {})
            for key, given in (("source", cover), ("time", time_), ("prompt", prompt), ("title", title),
                               ("subtitle", subtitle)):
                if given is not None:
                    options[key] = given
            if image is not None:
                content = await asyncio.to_thread(image.read_bytes)
                uploaded = await session.client.http.post(
                    f"/api/studio/jobs/{job_id}/plans/{p['id']}/query-media",
                    files={"file": (image.name, content, "image/png")}, data={"kind": "image", "purpose": "source"})
                from mindex.agent.loopback import _normalize

                result = _normalize(uploaded)
                if not result["ok"]:
                    raise StudioCliError(result["error"], result["status"])
                options.update(source="upload", media_id=result["data"]["media_id"])
            value = {"platforms": chosen, "cover": options}
        opts = {key: item for key, item in p["options"].items() if key != "references"}
        revision = w["revision"] if w["active_plan"] == p["id"] else plan_revision(p)
        saved = await session.call("PATCH", f"/api/studio/jobs/{job_id}/plans/{p['id']}",
                                   body={"revision": revision, "options": {**opts, "publish": value}})
        return {"job_id": job_id, "plan_id": p["id"], "publish": saved["plans"][p["id"]]["options"].get("publish")}

    result = run(ctx, body)
    if as_json:
        print_json(result)
    else:
        value = result["publish"]
        typer.echo("不发布" if not value else f"发布平台：{'、'.join(value['platforms'])}；封面 {value['cover']['source']}")


@publish_app.command("show")
def publish_show(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
                 plan: PlanOption = None, as_json: JsonOption = False):
    """查看发布设置与各平台的元数据、封面、发布包版本。

    示例：mindex studio publish show 3f2a
    """

    async def body(session):
        job_id, w, p = await load_workspace(session, job, plan)
        value = p["options"].get("publish")
        finals = p.get("finals") or {}
        items = []
        for name in (value or {}).get("platforms", []):
            meta = w["versions"].get(finals.get(f"metadata:{name}") or "")
            items.append({"platform": name, "metadata": finals.get(f"metadata:{name}"),
                          "cover": finals.get(f"cover:{name}"), "package": finals.get(f"package:{name}"),
                          "title": (meta or {}).get("data", {}).get("title")})
        return {"job_id": job_id, "plan_id": p["id"], "publish": value, "platforms": items}

    data = run(ctx, body)
    if as_json:
        print_json(data)
        return
    if not data["publish"]:
        typer.echo("该方案不发布")
    for item in data["platforms"]:
        typer.echo(f"{item['platform']}\t{'发布包已生成' if item['package'] else '未生成'}\t{item['title'] or ''}")


@publish_app.command("build")
def publish_build(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
                  plan: PlanOption = None, force: ForceOption = False,
                  watch: Annotated[bool, typer.Option("--watch", "-w", help="跟踪日志直到构建结束。")] = False,
                  as_json: JsonOption = False):
    """构建发布包（等同 build；有重新渲染导出时自动构建全部画幅）。

    示例：mindex studio publish build 3f2a --watch
    """

    async def peek(session):
        _, _, p = await load_workspace(session, job, plan)
        return any(item.get("mode") == "rerender" for item in p["options"].get("exports") or [])

    rerender = run(ctx, peek)
    ctx.invoke(build, ctx=ctx, job=job, plan=plan, force=force, watch=watch, all_canvases=rerender, as_json=as_json)


@publish_app.command("download")
def publish_download(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
                     platform: PlatformArgument,
                     output: Annotated[Path | None, typer.Option("--output", "-o", help="保存路径；缺省为当前目录。")] = None,
                     what: Annotated[str, typer.Option("--file", "-f", help="zip 发布包 / cover 封面 / metadata 元数据 JSON。")] = "zip",
                     plan: PlanOption = None, as_json: JsonOption = False):
    """下载某个平台的发布包 zip（或单独的封面、元数据），写入前校验 sha256。

    示例：mindex studio publish download 3f2a bilibili -o bilibili.zip
    """
    check_platform(platform)
    stages = {"zip": ("package", "zip"), "cover": ("cover", "cover"), "metadata": ("metadata", "metadata")}
    if what not in stages:
        raise typer.BadParameter("可选：zip / cover / metadata", param_hint="--file")

    async def body(session):
        import hashlib
        from urllib.parse import quote

        job_id, w, p = await load_workspace(session, job, plan)
        stage, name = stages[what]
        item = w["versions"].get((p.get("finals") or {}).get(f"{stage}:{platform}") or "")
        if not item or item["status"] != "complete":
            raise StudioCliError(f"{platform} 还没有生成，请先 mindex studio publish build")
        meta = item["files"][name]
        content = await session.fetch(f"/api/studio/jobs/{job_id}/artifacts/{quote(meta['path'], safe='/')}")
        if hashlib.sha256(content).hexdigest() != meta["sha256"]:
            raise StudioCliError("下载内容与版本记录的 sha256 不一致")
        target = output or Path(Path(meta["path"]).name)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(content)
        partial.replace(target)
        return {"job_id": job_id, "platform": platform, "path": str(target), "bytes": len(content)}

    result = run(ctx, body)
    if as_json:
        print_json(result)
    else:
        typer.echo(f"已保存 {result['path']}（{result['bytes']} 字节）")


@publish_app.command("cover")
def publish_cover(ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
                  platform: PlatformArgument, plan: PlanOption = None, as_json: JsonOption = False):
    """列出该平台成片的封面候选帧（按清晰度）；用 publish set --cover-time 选用。

    示例：mindex studio publish cover 3f2a youtube
    """
    check_platform(platform)

    async def body(session):
        job_id, _, p = await load_workspace(session, job, plan)
        return await session.get(f"/api/studio/jobs/{job_id}/plans/{p['id']}/cover-candidates", platform=platform)

    data = run(ctx, body)
    if as_json:
        print_json(data)
        return
    for item in data["items"]:
        typer.echo(f"{item['time']:g}s\t{item['shot_id']}\t清晰度 {item['sharpness']:g}")


@publish_app.command("metadata")
def publish_metadata(
    ctx: typer.Context, job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")], platform: PlatformArgument,
    title: Annotated[str | None, typer.Option("--title", help="修改标题。")] = None,
    description: Annotated[str | None, typer.Option("--description", help="修改简介正文（章节与署名由系统附加）。")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="标签，可重复；整体替换。")] = None,
    plan: PlanOption = None, as_json: JsonOption = False,
):
    """查看（不给修改项时）或人工修改某平台的发布元数据；修改后被钉住，封面与发布包重建。

    示例：mindex studio publish metadata 3f2a youtube --title 新标题 --tag 夜景 --tag 城市
    """
    check_platform(platform)
    changes = {key: value for key, value in (("title", title), ("description", description), ("tags", tag))
               if value is not None}

    async def body(session):
        from mindex.studio.workspace import revision as plan_revision

        job_id, w, p = await load_workspace(session, job, plan)
        if changes:
            revision = w["revision"] if w["active_plan"] == p["id"] else plan_revision(p)
            w = await session.call("PATCH", f"/api/studio/jobs/{job_id}/plans/{p['id']}/publish-metadata/{platform}",
                                   body={"revision": revision, **changes})
            p = w["plans"][p["id"]]
        item = w["versions"].get((p.get("finals") or {}).get(f"metadata:{platform}") or "")
        if not item:
            raise StudioCliError("还没有发布元数据，请先构建")
        return item["data"]

    data = run(ctx, body)
    if as_json:
        print_json(data)
        return
    typer.echo(f"标题：{data['title']}\n标签：{'、'.join(data['tags'])}\n简介：\n{data['description']}")


# ---------------------------------------------------------------- 曲库

music_app = typer.Typer(
    cls=SortedGroup,
    help="曲库：import 导入（授权来源、许可、署名必填），list 列出，show 查看，update 修改授权与情绪标签，"
    "analyze 重新分析节奏，recommend 按文案或分镜段落推荐，use 把曲目设为分镜的背景音乐提示点，delete 删除。\n\n"
    "示例：mindex studio music import bgm.mp3 --source 某曲库 --license 'CC BY 4.0' --attribution '作者 / 曲名'；"
    "mindex studio music recommend --job JOB --shot '#1'；mindex studio music use JOB '#1' TRACK。",
    no_args_is_help=True,
)
app.add_typer(music_app, name="music")
TrackArgument = Annotated[str, typer.Argument(help="曲目 ID 或唯一前缀（music list 列出）。")]


async def resolve_track(session: Session, track: str) -> str:
    items = (await session.get("/api/studio/music-library"))["items"]
    return unique([item["id"] for item in items], track, "曲目")


def track_line(item: dict) -> str:
    from mindex.studio.music_library import MOODS

    bpm = f"{item['bpm']:g} BPM" if item.get("bpm") else {"pending": "待分析", "failed": "分析失败"}.get(
        item["analysis_status"], "")
    moods = "、".join(MOODS[key][0] for key in item.get("moods") or [] if key in MOODS) or "无情绪标签"
    expiry = "已过期" if item.get("expired") else (f"授权至 {item['expires_at']}" if item.get("expires_at") else "长期授权")
    return f"{item['id'][:12]}\t{item['title']}\t{item['duration']:g}s\t{bpm}\t{moods}\t{expiry}"


@music_app.command("list")
def music_list(ctx: typer.Context, mood: Annotated[str | None, typer.Option("--mood", help="按情绪键筛选（calm、"
                                                                                       "energetic、tech 等）。")] = None,
               search: Annotated[str | None, typer.Option("--search", "-q", help="按曲名、作者、来源、标签搜索。")] = None,
               as_json: JsonOption = False):
    """列出曲库曲目：ID 前缀、曲名、时长、BPM、情绪、授权状态。

    示例：mindex studio music list --mood calm
    """

    async def body(session):
        params = {key: value for key, value in (("mood", mood), ("q", search)) if value}
        return (await session.get("/api/studio/music-library", **params))["items"]

    items = run(ctx, body)
    if as_json:
        print_json(items)
        return
    for item in items:
        typer.echo(track_line(item))


@music_app.command("show")
def music_show(ctx: typer.Context, track: TrackArgument, as_json: JsonOption = False):
    """查看曲目：授权信息、节奏分析与情绪标签。

    示例：mindex studio music show 3f2a --json
    """

    async def body(session):
        return await session.get(f"/api/studio/music-library/{await resolve_track(session, track)}")

    item = run(ctx, body)
    if as_json:
        print_json(item)
        return
    from mindex.agent.tools.studio_music import track_text

    typer.echo(track_text(item))


@music_app.command("import")
def music_import(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="音频文件（MP3/WAV/FLAC/OGG/M4A/AAC，≤20 分钟）。")],
    source: Annotated[str, typer.Option("--source", help="授权来源（必填），例如曲库网站或作者。")],
    license_: Annotated[str, typer.Option("--license", help="许可（必填），例如 CC BY 4.0 或商用授权编号。")],
    attribution: Annotated[str, typer.Option("--attribution", help="署名（必填），发布元数据会带上。")],
    title: Annotated[str | None, typer.Option("--title", help="曲名，缺省为文件名。")] = None,
    artist: Annotated[str | None, typer.Option("--artist", help="作者。")] = None,
    expires: Annotated[str | None, typer.Option("--expires", help="授权有效期 YYYY-MM-DD（含当天），缺省长期。")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="标签，可重复。")] = None,
    no_ai: Annotated[bool, typer.Option("--no-ai", help="不调用文案模型打情绪标签（按节奏特征推断）。")] = False,
    as_json: JsonOption = False,
):
    """导入曲目并分析节奏；相同音频只保留一份（返回已有曲目）。

    示例：mindex studio music import bgm.mp3 --source Pixabay --license 'Pixabay License' --attribution 'Artist - Song'
    """
    import mimetypes

    if not path.is_file():
        raise typer.BadParameter(f"找不到文件：{path}", param_hint="PATH")
    fields = {"source": source, "license": license_, "attribution": attribution, "title": title or "",
              "artist": artist or "", "expires_at": expires or "", "tags": ",".join(tag or []),
              "ai": "false" if no_ai else "true"}

    async def body(session):
        content = await asyncio.to_thread(path.read_bytes)
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = await session.client.http.post("/api/studio/music-library", data=fields,
                                                  files={"file": (path.name, content, kind)})
        from mindex.agent.loopback import _normalize

        result = _normalize(response)
        if not result["ok"]:
            raise StudioCliError(result["error"], result["status"])
        return result["data"]

    item = run(ctx, body)
    if as_json:
        print_json(item)
        return
    typer.echo(("曲库中已有相同音频：" if item.get("duplicate") else "已导入：") + track_line(item))


@music_app.command("update")
def music_update(
    ctx: typer.Context,
    track: TrackArgument,
    title: Annotated[str | None, typer.Option("--title", help="新曲名。")] = None,
    artist: Annotated[str | None, typer.Option("--artist", help="新作者。")] = None,
    source: Annotated[str | None, typer.Option("--source", help="新授权来源。")] = None,
    license_: Annotated[str | None, typer.Option("--license", help="新许可。")] = None,
    attribution: Annotated[str | None, typer.Option("--attribution", help="新署名。")] = None,
    expires: Annotated[str | None, typer.Option("--expires", help="授权有效期 YYYY-MM-DD；none 改为长期。")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="标签，可重复；整体替换。")] = None,
    mood: Annotated[list[str] | None, typer.Option("--mood", help="情绪键，可重复（最多 3 个）；整体替换。")] = None,
    as_json: JsonOption = False,
):
    """修改曲目的授权信息、标签与情绪标签。

    示例：mindex studio music update 3f2a --mood calm --mood warm --expires 2027-12-31
    """
    changes = {key: value for key, value in (("title", title), ("artist", artist), ("source", source),
                                             ("license", license_), ("attribution", attribution), ("tags", tag),
                                             ("moods", mood)) if value is not None}
    if expires is not None:
        changes["expires_at"] = None if expires.lower() == "none" else expires
    if not changes:
        raise typer.BadParameter("至少给出一个要修改的字段", param_hint="--title/--license/--mood/…")

    async def body(session):
        track_id = await resolve_track(session, track)
        return await session.call("PUT", f"/api/studio/music-library/{track_id}", body=changes)

    item = run(ctx, body)
    if as_json:
        print_json(item)
    else:
        typer.echo("已修改：" + track_line(item))


@music_app.command("analyze")
def music_analyze(ctx: typer.Context, track: TrackArgument,
                  keep_moods: Annotated[bool, typer.Option("--keep-moods", help="保留现有情绪标签（只重新分析节奏）。")] = False,
                  no_ai: Annotated[bool, typer.Option("--no-ai", help="不调用文案模型，按节奏特征推断情绪。")] = False,
                  as_json: JsonOption = False):
    """重新分析曲目的 BPM、节拍、能量与亮度，并（缺省）重新打情绪标签。

    示例：mindex studio music analyze 3f2a --keep-moods
    """

    async def body(session):
        track_id = await resolve_track(session, track)
        return await session.post(f"/api/studio/music-library/{track_id}/analyze",
                                  {"ai": not no_ai, "retag": not keep_moods})

    item = run(ctx, body)
    if as_json:
        print_json(item)
    else:
        typer.echo(f"已分析：{track_line(item)}（{item.get('beat_count')} 个节拍）")


@music_app.command("recommend")
def music_recommend(
    ctx: typer.Context,
    text: Annotated[str | None, typer.Option("--text", help="段落文案；与 --job/--shot 二选一。")] = None,
    job: Annotated[str | None, typer.Option("--job", help="任务 ID 或唯一前缀（与 --shot 一起使用）。")] = None,
    shot: Annotated[str | None, typer.Option("--shot", help="背景音乐段落起始分镜：ID、前缀或 #序号。")] = None,
    plan: PlanOption = None,
    seconds: Annotated[float | None, typer.Option("--seconds", help="段落时长（秒）；按分镜推荐时缺省为预估时长。")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=50, help="返回条数。")] = 5,
    no_ai: Annotated[bool, typer.Option("--no-ai", help="不调用文案模型，按关键词推断情绪。")] = False,
    as_json: JsonOption = False,
):
    """按段落文案推荐曲目：推断情绪与节奏区间，按情绪、BPM、时长排序（授权过期的曲目不参与）。

    示例：mindex studio music recommend --job 3f2a --shot '#1'；mindex studio music recommend --text '科技发布会开场'
    """
    if not text and not (job and shot):
        raise typer.BadParameter("需要 --text，或同时给出 --job 与 --shot", param_hint="--text/--job/--shot")

    async def body(session):
        request = {"limit": limit, "ai": not no_ai, "seconds": seconds}
        if text:
            request["text"] = text
        else:
            job_id, w, p = await load_workspace(session, job, plan)
            order = w["versions"][p["board"]]["data"]["order"] if p.get("board") else []
            request.update(job_id=job_id, plan_id=p["id"], shot_id=resolve_shot_ref(order, shot))
        return await session.post("/api/studio/music-library/recommend",
                                  {key: value for key, value in request.items() if value is not None})

    result = run(ctx, body)
    if as_json:
        print_json(result)
        return
    from mindex.studio.music_library import MOODS

    moods = "、".join(MOODS[key][0] for key in result["moods"]) or "未能推断"
    bpm = "–".join(map(str, result["bpm_range"])) if result["bpm_range"] else "不限"
    typer.echo(f"推断情绪：{moods}；节奏：{bpm} BPM")
    for item in result["items"]:
        typer.echo(f"{item['score']:g}\t{track_line(item)}\t{'；'.join(item['reasons'])}")


@music_app.command("use")
def music_use(
    ctx: typer.Context,
    job: Annotated[str, typer.Argument(help="任务 ID 或唯一前缀。")],
    shot: ShotArgument,
    track: TrackArgument,
    volume: Annotated[float, typer.Option("--volume", min=0, max=1, help="混音音量 0–1。")] = 0.25,
    plan: PlanOption = None,
    as_json: JsonOption = False,
):
    """把曲库曲目设为分镜的背景音乐提示点（之后的分镜顺延，直到下一个提示点）。

    示例：mindex studio music use 3f2a '#1' 9c1e --volume 0.3
    """

    async def body(session):
        track_id = await resolve_track(session, track)
        job_id, w, p, order, shots = await board_shots(session, job, plan)
        sid = resolve_shot_ref(order, shot)
        target = next(item for item in shots if item["id"] == sid)
        target["music"] = {"kind": "library", "track_id": track_id, "volume": volume}
        from mindex.studio.workspace import revision as plan_revision

        revision = w["revision"] if w["active_plan"] == p["id"] else plan_revision(p)
        await session.call("PATCH", f"/api/studio/jobs/{job_id}/plans/{p['id']}",
                           body={"revision": revision, "shots": shots})
        return {"job_id": job_id, "plan_id": p["id"], "shot_id": sid, "track_id": track_id, "volume": volume}

    result = run(ctx, body)
    if as_json:
        print_json(result)
    else:
        typer.echo(f"分镜 {result['shot_id']} 的背景音乐改为曲库曲目 {result['track_id'][:12]}（音量 {volume:g}）；"
                   "下次构建复制曲目并重新合成成片")


@music_app.command("delete")
def music_delete(ctx: typer.Context, track: TrackArgument,
                 yes: Annotated[bool, typer.Option("--yes", "-y", help="不再确认。")] = False):
    """删除曲目记录与音频文件（已构建的背景音乐版本保留副本）。被分镜选用时拒绝（退出码 2）。

    示例：mindex studio music delete 3f2a --yes
    """
    if not yes and not typer.confirm(f"删除曲目 {track}？"):
        raise typer.Exit(1)

    async def body(session):
        track_id = await resolve_track(session, track)
        await session.call("DELETE", f"/api/studio/music-library/{track_id}")
        return track_id

    track_id = run(ctx, body)
    typer.echo(f"已删除曲目 {track_id[:12]}")


# ---------------------------------------------------------------- 第三方工具源

tools_app = typer.Typer(
    cls=SortedGroup,
    help="第三方工具源：list 列出，show 查看，add 登记或整体替换（JSON 文件或常用选项），test 测试连接，delete 删除。"
    "分镜画面来源设为 {kind: tool, tool_id, tool} 时调用。\n\n"
    "示例：mindex studio tools add gen --name 生成服务 --http https://gen.example.com --tool make_video:video "
    "--header 'Authorization=secret:token' --secret token=GEN_TOKEN；mindex studio tools test gen。",
    no_args_is_help=True,
)
app.add_typer(tools_app, name="tools")
SourceArgument = Annotated[str, typer.Argument(help="工具源 ID（小写字母、数字与连字符）。")]


def tool_source_line(item: dict) -> str:
    tools = ",".join(f"{tool['name']}:{tool['output']}{':upload' if tool['allow_upload'] else ''}" for tool in item["tools"])
    missing = f"\t缺少密钥 {','.join(item['missing_secrets'])}" if item.get("missing_secrets") else ""
    return f"{item['id']}\t{item['name']}\t{item['protocol']}\t{tools}{missing}"


def parse_pairs(values: list[str] | None, option: str) -> dict[str, str]:
    found = {}
    for value in values or []:
        key, sep, item = value.partition("=")
        if not sep or not key.strip():
            raise typer.BadParameter("格式为 名称=值", param_hint=option)
        found[key.strip()] = item
    return found


@tools_app.command("list")
def tools_list(ctx: typer.Context, as_json: JsonOption = False):
    """列出工具源：ID、名称、协议、白名单工具（名称:产物[:upload]）与缺少的密钥。

    示例：mindex studio tools list
    """

    async def body(session):
        return (await session.get("/api/studio/tool-sources"))["items"]

    items = run(ctx, body)
    if as_json:
        print_json(items)
        return
    for item in items:
        typer.echo(tool_source_line(item))


@tools_app.command("show")
def tools_show(ctx: typer.Context, source: SourceArgument, as_json: JsonOption = False):
    """查看工具源登记（密钥只显示名称与是否已填写）。

    示例：mindex studio tools show gen --json
    """

    async def body(session):
        return await session.get(f"/api/studio/tool-sources/{source}")

    item = run(ctx, body)
    if as_json:
        print_json(item)
        return
    from mindex.agent.tools.studio_tools import connection_text

    typer.echo(tool_source_line(item))
    typer.echo(f"  连接：{connection_text(item['protocol'], item['connection'])}")
    typer.echo(f"  产物主机：{', '.join(item['artifact_hosts']) or '（http 协议只允许 base_url 的主机）'}")
    typer.echo("  密钥：" + (", ".join(f"{entry['name']}{'' if entry['set'] else '（未填写）'}" for entry in item["secrets"])
                          or "无"))


@tools_app.command("add")
def tools_add(
    ctx: typer.Context,
    source: SourceArgument,
    file: Annotated[Path | None, typer.Option("--file", "-f", help="完整登记 JSON（与 PUT /api/studio/tool-sources/ID 的请求体相同）。")] = None,
    name: Annotated[str | None, typer.Option("--name", help="显示名称（缺省为 ID）。")] = None,
    http: Annotated[str | None, typer.Option("--http", help="HTTP manifest 服务的 base_url。")] = None,
    mcp_url: Annotated[str | None, typer.Option("--mcp-url", help="streamable-http MCP 服务器地址。")] = None,
    stdio: Annotated[str | None, typer.Option("--stdio", help="本机 MCP 服务器命令（绝对路径）。")] = None,
    arg: Annotated[list[str] | None, typer.Option("--arg", help="--stdio 命令的参数，可重复。")] = None,
    env: Annotated[list[str] | None, typer.Option("--env", help="--stdio 的环境变量 名称=值（值可写 secret:名称），可重复。")] = None,
    header: Annotated[list[str] | None, typer.Option("--header", help="请求头 名称=值（值可写 secret:名称），可重复。")] = None,
    tool: Annotated[list[str] | None, typer.Option("--tool", help="白名单工具 名称[:video|image][:upload]，可重复。")] = None,
    host: Annotated[list[str] | None, typer.Option("--host", help="允许下载产物的主机（可带端口），可重复。")] = None,
    secret: Annotated[list[str] | None, typer.Option("--secret", help="密钥 名称=环境变量名：从该环境变量读取密钥值"
                                                                  "（不在命令行出现明文），可重复。")] = None,
    as_json: JsonOption = False,
):
    """登记或整体替换工具源。给 --file 时以文件为准（--secret 仍可追加密钥）；否则用 --http / --mcp-url / --stdio 之一。

    示例：

    \b
      mindex studio tools add gen --http https://gen.example.com --tool make_video:video --tool still:image:upload \\
        --header 'Authorization=secret:token' --secret token=GEN_TOKEN
      mindex studio tools add local --stdio /opt/tools/bin/mcp-server --arg --quiet --tool render_scene:video
      mindex studio tools add gen -f gen.json
    """
    import json

    if file is not None:
        try:
            spec = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(f"无法读取 JSON：{exc}", param_hint="--file") from exc
    else:
        chosen = [item for item in (http, mcp_url, stdio) if item]
        if len(chosen) != 1:
            raise typer.BadParameter("需要且只需要 --http、--mcp-url、--stdio 之一（或用 --file）", param_hint="--http")
        if not tool:
            raise typer.BadParameter("至少给出一个 --tool", param_hint="--tool")
        entries = []
        for value in tool:
            parts = value.split(":")
            output = next((part for part in parts[1:] if part in ("video", "image")), "video")
            entries.append({"name": parts[0], "output": output, "allow_upload": "upload" in parts[1:]})
        headers = parse_pairs(header, "--header")
        spec = {"name": name or source, "tools": entries, "artifact_hosts": list(host or [])}
        if http:
            spec.update(protocol="http", http={"base_url": http, "headers": headers})
        elif mcp_url:
            spec.update(protocol="mcp-http", mcp_http={"url": mcp_url, "headers": headers})
        else:
            spec.update(protocol="mcp-stdio", stdio={"command": stdio, "args": list(arg or []),
                                                     "env": parse_pairs(env, "--env")})
    for key, variable in parse_pairs(secret, "--secret").items():
        value = os.environ.get(variable)
        if not value:
            raise typer.BadParameter(f"环境变量 {variable} 未设置", param_hint="--secret")
        spec.setdefault("secrets", {})[key] = value

    async def body(session):
        return await session.call("PUT", f"/api/studio/tool-sources/{source}", body=spec)

    item = run(ctx, body)
    if as_json:
        print_json(item)
    else:
        typer.echo("已登记：" + tool_source_line(item))


@tools_app.command("test")
def tools_test(ctx: typer.Context, source: SourceArgument, as_json: JsonOption = False):
    """测试连接并列出服务端工具（不提交任务；mcp-stdio 会启动登记的本机进程）。连接失败时退出码 1。

    示例：mindex studio tools test gen
    """

    async def body(session):
        return await session.post(f"/api/studio/tool-sources/{source}/test", {})

    result = run(ctx, body)
    if as_json:
        print_json(result)
    elif result["ok"]:
        names = ", ".join(item["name"] for item in result.get("tools") or []) or "（服务端未提供工具列表）"
        typer.echo(f"连接成功（{result['elapsed_ms']} ms）：{names}")
        if result.get("missing"):
            typer.echo(f"白名单中有、服务端没有：{', '.join(result['missing'])}")
    else:
        typer.echo(f"连接失败：{result['error']}", err=True)
    if not result["ok"]:
        raise typer.Exit(1)


@tools_app.command("delete")
def tools_delete(ctx: typer.Context, source: SourceArgument,
                 yes: Annotated[bool, typer.Option("--yes", "-y", help="不再确认。")] = False):
    """删除工具源登记（已调用的产物版本保留）。被分镜使用时拒绝（退出码 2）。

    示例：mindex studio tools delete gen --yes
    """
    if not yes and not typer.confirm(f"删除工具源 {source}？"):
        raise typer.Exit(1)

    async def body(session):
        return await session.call("DELETE", f"/api/studio/tool-sources/{source}")

    run(ctx, body)
    typer.echo(f"已删除工具源 {source}")
