"""`mindex topics` 命令组：选题挖掘（与页面、REST、智能助手和 MCP 共用 ``mindex.topics`` 服务层）。

命令直接读写数据目录，不需要 ``mindex serve`` 在运行；与服务进程之间经 ``topics`` 锁互斥。
输出均为 JSON；失败时错误写到标准错误并以退出码 1 结束。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from mindex.shared.cli_support import SortedGroup, print_json

app = typer.Typer(
    cls=SortedGroup,
    help="选题挖掘：领域档案、数据源、挖掘、选题卡片、建创作任务与素材采集清单。\n\n"
    "与创作页「选题挖掘」、/api/topics、智能助手 topics 领域工具共用同一服务层，详见 docs/topics.md。"
    "示例：mindex topics profiles list；mindex topics discover PROFILE_ID。",
    no_args_is_help=True,
)
profiles_app = typer.Typer(
    cls=SortedGroup,
    help="领域档案：领域、子方向、受众、账号定位、禁区、关键词、打分权重与每日定时。\n\n"
    "示例：mindex topics profiles create --name 财经号 --domain 金融 --taboo 不荐股。",
    no_args_is_help=True,
)
app.add_typer(profiles_app, name="profiles")
sources_app = typer.Typer(
    cls=SortedGroup,
    help="数据源：rss（RSS/Atom 订阅）、hotlist（热榜 JSON 接口）、brave（Brave 搜索）、calendar（日历）、"
    "local（本地素材库）。只请求你配置的地址，不内置爬虫。\n\n"
    "示例：mindex topics sources add tp_0123456789ab --kind rss --name 财经要闻 --url https://example.com/feed.xml。",
    no_args_is_help=True,
)
app.add_typer(sources_app, name="sources")


def _store(ctx: typer.Context):
    from mindex.shared.storage import Store

    return Store.open(ctx.obj)


def run(action):
    """执行服务层调用，把已知错误转成中文提示与退出码 1。"""
    from mindex.admin.curation import EntityNotFound
    from mindex.models.adapters import ModelError

    try:
        return action()
    except (EntityNotFound, ModelError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def _read_json(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"无法读取 JSON 文件：{exc}", param_hint="--file") from exc
    if not isinstance(value, dict):
        raise typer.BadParameter("JSON 文件必须是对象", param_hint="--file")
    return value


ProfileFile = Annotated[Path | None, typer.Option("--file", help="档案 JSON（字段同 /api/topics/profiles），命令行选项覆盖其中的同名字段。")]
Name = Annotated[str | None, typer.Option(help="档案名称。")]
Domain = Annotated[str | None, typer.Option(help="领域，如 金融、科技、历史。")]
Subtopic = Annotated[list[str] | None, typer.Option("--subtopic", help="子方向，可重复。")]
Audience = Annotated[str | None, typer.Option(help="目标受众。")]
Positioning = Annotated[str | None, typer.Option(help="账号定位与表达风格。")]
Taboo = Annotated[list[str] | None, typer.Option("--taboo", help="禁区，可重复；也会作为写作约束进入文案提示词。")]
Keyword = Annotated[list[str] | None, typer.Option("--keyword", help="检索与搜索关键词，可重复。")]
Canvas = Annotated[str | None, typer.Option(help="从选题建任务时的默认画幅：16:9、9:16、1:1。")]
ScheduleTime = Annotated[str | None, typer.Option("--schedule-time", help="每日定时挖掘的本地时刻 HH:MM。")]
ScheduleCount = Annotated[int | None, typer.Option("--schedule-count", help="每次为前 N 名写大纲。")]
ScheduleEnabled = Annotated[bool | None, typer.Option("--schedule/--no-schedule", help="开启或关闭每日定时挖掘。")]


def _profile_values(file, name, domain, subtopic, audience, positioning, taboo, keyword, canvas,
                    schedule_time, schedule_count, schedule_enabled, current_schedule=None) -> dict:
    values = _read_json(file)
    given = {"name": name, "domain": domain, "subtopics": subtopic, "audience": audience,
             "positioning": positioning, "taboos": taboo, "keywords": keyword, "canvas": canvas}
    values.update({key: value for key, value in given.items() if value is not None})
    if schedule_time is not None or schedule_count is not None or schedule_enabled is not None:
        schedule = dict(values.get("schedule") or current_schedule or {})
        for key, value in (("time", schedule_time), ("count", schedule_count), ("enabled", schedule_enabled)):
            if value is not None:
                schedule[key] = value
        values["schedule"] = schedule
    return values


@profiles_app.command("list")
def profiles_list(ctx: typer.Context):
    """列出全部领域档案。

    示例：

    \b
      mindex topics profiles list
    """
    from mindex.topics import profiles

    print_json({"items": profiles.list_profiles(_store(ctx))})


@profiles_app.command("show")
def profiles_show(ctx: typer.Context, profile_id: Annotated[str, typer.Argument(help="档案 ID（tp_ 开头）。")]):
    """显示一个领域档案。

    示例：

    \b
      mindex topics profiles show tp_0123456789ab
    """
    from mindex.topics import profiles

    print_json(run(lambda: profiles.get_profile(_store(ctx), profile_id)))


@profiles_app.command("create")
def profiles_create(
    ctx: typer.Context, file: ProfileFile = None, name: Name = None, domain: Domain = None, subtopic: Subtopic = None,
    audience: Audience = None, positioning: Positioning = None, taboo: Taboo = None, keyword: Keyword = None,
    canvas: Canvas = None, schedule_time: ScheduleTime = None, schedule_count: ScheduleCount = None,
    schedule_enabled: ScheduleEnabled = None,
    material_source: Annotated[list[str] | None, typer.Option("--material-source", help="素材库 ID，可重复；与素材组取交集。")]=None,
    source_group: Annotated[list[str] | None, typer.Option("--source-group", help="素材组 ID，可重复，多组取并集。")]=None,
):
    """新建领域档案（--name 与 --domain 必填，可来自 --file）。

    示例：

    \b
      mindex topics profiles create --name 财经号 --domain 金融 --subtopic 宏观 --taboo 不荐股
      mindex topics profiles create --file profile.json
    """
    from mindex.topics import profiles
    from mindex.topics.models import ProfileBody

    values = _profile_values(file, name, domain, subtopic, audience, positioning, taboo, keyword, canvas,
                             schedule_time, schedule_count, schedule_enabled)
    values.update({k: v for k, v in {"source_ids": material_source, "source_group_ids": source_group}.items() if v is not None})
    print_json(run(lambda: profiles.create_profile(_store(ctx), ProfileBody.model_validate(values))))


@profiles_app.command("update")
def profiles_update(
    ctx: typer.Context, profile_id: Annotated[str, typer.Argument(help="档案 ID。")], file: ProfileFile = None,
    name: Name = None, domain: Domain = None, subtopic: Subtopic = None, audience: Audience = None,
    positioning: Positioning = None, taboo: Taboo = None, keyword: Keyword = None, canvas: Canvas = None,
    schedule_time: ScheduleTime = None, schedule_count: ScheduleCount = None, schedule_enabled: ScheduleEnabled = None,
    material_source: Annotated[list[str] | None, typer.Option("--material-source", help="素材库 ID，可重复；与素材组取交集。")]=None,
    source_group: Annotated[list[str] | None, typer.Option("--source-group", help="素材组 ID，可重复，多组取并集。")]=None,
):
    """修改领域档案：只改给出的字段（列表类选项整体替换）。

    示例：

    \b
      mindex topics profiles update tp_0123456789ab --taboo 不荐股 --taboo 不谈加密货币
      mindex topics profiles update tp_0123456789ab --schedule --schedule-time 08:30
    """
    from mindex.topics import profiles
    from mindex.topics.models import ProfileChanges

    store = _store(ctx)

    def action():
        current = profiles.get_profile(store, profile_id)
        values = _profile_values(file, name, domain, subtopic, audience, positioning, taboo, keyword, canvas,
                                 schedule_time, schedule_count, schedule_enabled, current["schedule"])
        values.update({k: v for k, v in {"source_ids": material_source, "source_group_ids": source_group}.items() if v is not None})
        if not values:
            raise ValueError("至少给出一个要修改的字段")
        return profiles.update_profile(store, profile_id, ProfileChanges.model_validate(values))

    print_json(run(action))


@profiles_app.command("delete")
def profiles_delete(
    ctx: typer.Context, profile_id: Annotated[str, typer.Argument(help="档案 ID。")],
    yes: Annotated[bool, typer.Option("--yes", help="确认删除。")] = False,
):
    """删除领域档案及其数据源、候选条目、选题卡片、采集清单与挖掘记录（已建的创作任务不受影响）。

    示例：

    \b
      mindex topics profiles delete tp_0123456789ab --yes
    """
    from mindex.topics import profiles

    if not yes:
        raise typer.BadParameter("删除会一并删除该档案的数据源、选题与采集清单；确认请加 --yes", param_hint="--yes")
    print_json(run(lambda: profiles.delete_profile(_store(ctx), profile_id)))


# ---------------------------------------------------------------- 数据源与抓取

SourceConfig = Annotated[str | None, typer.Option("--config", help="来源配置 JSON（字段按 --kind，见 docs/topics.md）。")]
SourceUrl = Annotated[str | None, typer.Option("--url", help="rss/hotlist/calendar 的地址（写入 config.url）。")]
SecretEnv = Annotated[str | None, typer.Option("--secret-env", help="从该环境变量读取密钥（如 Brave API Key），避免出现在命令历史里。")]


def _source_config(config: str | None, url: str | None, current: dict | None = None) -> dict | None:
    if config is None and url is None:
        return None
    value = dict(current or {})
    if config is not None:
        try:
            parsed = json.loads(config)
        except ValueError as exc:
            raise typer.BadParameter(f"不是有效的 JSON：{exc}", param_hint="--config") from exc
        if not isinstance(parsed, dict):
            raise typer.BadParameter("必须是 JSON 对象", param_hint="--config")
        value = parsed
    if url is not None:
        value["url"] = url
    return value


def _secret(env: str | None) -> str | None:
    if env is None:
        return None
    import os

    value = os.environ.get(env)
    if not value:
        raise typer.BadParameter(f"环境变量 {env} 未设置或为空", param_hint="--secret-env")
    return value


@sources_app.command("list")
def sources_list(ctx: typer.Context, profile_id: Annotated[str, typer.Argument(help="档案 ID。")]):
    """列出档案的数据源（密钥只显示掩码）。

    示例：

    \b
      mindex topics sources list tp_0123456789ab
    """
    from mindex.topics import datasources

    print_json({"items": run(lambda: datasources.list_sources(_store(ctx), profile_id))})


@sources_app.command("add")
def sources_add(
    ctx: typer.Context, profile_id: Annotated[str, typer.Argument(help="档案 ID。")],
    kind: Annotated[str, typer.Option(help="rss / hotlist / brave / calendar / local。")],
    name: Annotated[str, typer.Option(help="数据源名称。")],
    config: SourceConfig = None, url: SourceUrl = None, secret_env: SecretEnv = None,
    disabled: Annotated[bool, typer.Option("--disabled", help="添加后先不启用。")] = False,
):
    """给档案添加数据源。

    示例：

    \b
      mindex topics sources add tp_0123456789ab --kind rss --name 财经要闻 --url https://example.com/feed.xml
      mindex topics sources add tp_0123456789ab --kind local --name 本地素材库
    """
    from mindex.topics import datasources

    body = {"kind": kind, "name": name, "enabled": not disabled, "config": _source_config(config, url) or {},
            "secret": _secret(secret_env)}
    print_json(run(lambda: datasources.create_source(_store(ctx), profile_id,
                                                     datasources.SourceBody.model_validate(body))))


@sources_app.command("update")
def sources_update(
    ctx: typer.Context, source_id: Annotated[str, typer.Argument(help="数据源 ID（ts_ 开头）。")],
    name: Annotated[str | None, typer.Option(help="新名称。")] = None,
    config: SourceConfig = None, url: SourceUrl = None, secret_env: SecretEnv = None,
    clear_secret: Annotated[bool, typer.Option("--clear-secret", help="清除已保存的密钥。")] = False,
    enabled: Annotated[bool | None, typer.Option("--enable/--disable", help="启用或停用。")] = None,
):
    """修改数据源：只改给出的项；--url 只替换 config.url，其余配置保留。

    示例：

    \b
      mindex topics sources update ts_0123456789ab --secret-env BRAVE_API_KEY
    """
    from mindex.topics import datasources

    store = _store(ctx)

    def action():
        current = datasources.get_source(store, source_id)
        changes = {"name": name, "enabled": enabled, "config": _source_config(config, url, current["config"])}
        changes = {k: v for k, v in changes.items() if v is not None}
        if clear_secret:
            changes["secret"] = ""
        elif secret_env:
            changes["secret"] = _secret(secret_env)
        if not changes:
            raise ValueError("至少给出一个要修改的项")
        return datasources.update_source(store, source_id, datasources.SourceChanges.model_validate(changes))

    print_json(run(action))


@sources_app.command("remove")
def sources_remove(
    ctx: typer.Context, source_id: Annotated[str, typer.Argument(help="数据源 ID。")],
    yes: Annotated[bool, typer.Option("--yes", help="确认删除。")] = False,
):
    """删除数据源记录（已抓到的候选条目保留，随过期清理）。

    示例：

    \b
      mindex topics sources remove ts_0123456789ab --yes
    """
    from mindex.topics import datasources

    if not yes:
        raise typer.BadParameter("确认删除请加 --yes", param_hint="--yes")
    print_json(run(lambda: datasources.delete_source(_store(ctx), source_id)))


@sources_app.command("test")
def sources_test(ctx: typer.Context, source_id: Annotated[str, typer.Argument(help="数据源 ID。")]):
    """试抓一次并显示前 20 条，不入库；失败时 ok 为 false 并给出原因。

    示例：

    \b
      mindex topics sources test ts_0123456789ab
    """
    from mindex.topics import datasources

    result = run(lambda: datasources.try_source(ctx.obj, _store(ctx), source_id))
    print_json(result)
    if not result["ok"]:
        raise typer.Exit(1)


@sources_app.command("import-calendar")
def sources_import_calendar(
    ctx: typer.Context, source_id: Annotated[str, typer.Argument(help="日历数据源 ID。")],
    file: Annotated[Path | None, typer.Argument(help="ICS 或 CSV 文件（UTF-8）。")] = None,
    sample: Annotated[str | None, typer.Option("--sample", help="载入内置样例：finance / tech / history。")] = None,
    append: Annotated[bool, typer.Option("--append", help="追加到已有事件；缺省替换。")] = False,
):
    """为日历数据源导入事件（ICS 的 VEVENT，或 CSV：date,title[,desc,kind,recurring]），或载入内置样例。

    示例：

    \b
      mindex topics sources import-calendar ts_0123456789ab events.ics
      mindex topics sources import-calendar ts_0123456789ab --sample history
    """
    from mindex.topics import datasources

    if (file is None) == (sample is None):
        raise typer.BadParameter("请给出日历文件，或用 --sample 选择样例（二选一）")
    store = _store(ctx)
    if sample:
        print_json(run(lambda: datasources.import_sample(store, source_id, sample, append=append)))
        return
    try:
        raw = file.read_bytes()
    except OSError as exc:
        raise typer.BadParameter(f"无法读取文件：{exc}") from exc
    if len(raw) > ctx.obj.topics.fetch.max_bytes:
        raise typer.BadParameter(f"文件超过 {ctx.obj.topics.fetch.max_bytes // 1024} KB 上限")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise typer.BadParameter("文件不是 UTF-8 文本") from exc
    fmt = "ics" if file.suffix.lower() == ".ics" else "csv" if file.suffix.lower() == ".csv" else None
    print_json(run(lambda: datasources.import_calendar(store, source_id, text, fmt=fmt, append=append)))


@app.command("fetch")
def fetch(
    ctx: typer.Context, profile_id: Annotated[str, typer.Argument(help="档案 ID。")],
    source: Annotated[list[str] | None, typer.Option("--source", help="只抓指定数据源，可重复；省略抓全部启用的来源。")] = None,
):
    """立即抓取档案的数据源并入库去重（不归纳选题；归纳与打分用 discover）。

    示例：

    \b
      mindex topics fetch tp_0123456789ab
    """
    from mindex.topics import datasources

    print_json(run(lambda: datasources.fetch_profile(ctx.obj, _store(ctx), profile_id, source_ids=source)))


@app.command("items")
def items(
    ctx: typer.Context, profile_id: Annotated[str, typer.Argument(help="档案 ID。")],
    limit: Annotated[int, typer.Option(min=1, max=500, help="最多条数。")] = 50,
    source: Annotated[str | None, typer.Option("--source", help="只看某个数据源。")] = None,
):
    """列出档案最近的候选条目（标题、摘要、链接、发布时间）。

    示例：

    \b
      mindex topics items tp_0123456789ab --limit 20
    """
    from mindex.topics import items as topic_items
    from mindex.topics import profiles

    store = _store(ctx)

    def action():
        profiles.get_profile(store, profile_id)
        return {"items": topic_items.list_items(store, profile_id, limit=limit, source_id=source)}

    print_json(run(action))


# ---------------------------------------------------------------- 挖掘任务与选题卡片

runs_app = typer.Typer(
    cls=SortedGroup,
    help="挖掘任务：list 查看档案的运行记录，show 查看进度与结果，cancel 取消，retry 重试失败或中断的任务。\n\n"
    "示例：mindex topics runs list tp_0123456789ab；mindex topics runs show tr_0123456789ab。",
    no_args_is_help=True,
)
app.add_typer(runs_app, name="runs")


class _Runtime:
    """CLI 已合并设置页覆盖层，直接返回同一份配置。"""

    def __init__(self, config):
        self.config = config

    def current(self):
        return self.config


def _runs(ctx: typer.Context):
    from mindex.topics.runner import TopicRuns

    return TopicRuns(_Runtime(ctx.obj), _store(ctx))


def _http(action):
    """服务层在 runner 中沿用 HTTPException（REST 共用）；CLI 转成中文提示。"""
    from fastapi import HTTPException

    def wrapped():
        try:
            return action()
        except HTTPException as exc:
            raise ValueError(str(exc.detail)) from exc

    return wrapped


@app.command("discover")
def discover(
    ctx: typer.Context, profile_id: Annotated[str, typer.Argument(help="档案 ID。")],
    skip_fetch: Annotated[bool, typer.Option("--skip-fetch", help="不重新抓取，只用已有的候选条目。")] = False,
    outline_count: Annotated[int | None, typer.Option("--outline-count", min=0, max=50,
                                                      help="为前 N 名写大纲；缺省取档案定时设置的条数。")] = None,
):
    """在本进程前台运行一次选题挖掘：抓取 → 聚类 → 归纳 → 打分与素材覆盖 → 前 N 名写大纲。

    进度写到标准错误，完成后输出任务记录（JSON）。与 mindex serve 共用 topics 锁：已有挖掘任务在运行时退出码为 1。
    需要文案（或 topics.llm）模型与嵌入服务；嵌入不可用时按标题分组并在结果里告警。

    示例：

    \b
      mindex topics discover tp_0123456789ab
      mindex topics discover tp_0123456789ab --skip-fetch --outline-count 5
    """
    from mindex.topics.runner import RunOptions

    runner = _runs(ctx)
    original = runner.save

    def save(task_id, field, value):
        if field == "progress" and value.get("message"):
            typer.echo(f"[{value.get('stage', '')}] {value['message']}", err=True)
        return original(task_id, field, value)

    runner.save = save
    options = RunOptions(skip_fetch=skip_fetch, outline_count=outline_count)
    result = run(lambda: runner.run_here(profile_id, options))
    from mindex.topics.runner import get_run

    view = get_run(runner.store, result["id"])
    print_json(view)
    if view["status"] != "complete":
        raise typer.Exit(1)


@runs_app.command("list")
def runs_list(ctx: typer.Context, profile_id: Annotated[str, typer.Argument(help="档案 ID。")],
              limit: Annotated[int, typer.Option(min=1, max=200, help="最多条数。")] = 20):
    """列出档案的挖掘任务（新的在前）。

    示例：

    \b
      mindex topics runs list tp_0123456789ab
    """
    from mindex.topics.runner import list_runs

    print_json({"items": run(lambda: list_runs(_store(ctx), profile_id, limit))})


@runs_app.command("show")
def runs_show(ctx: typer.Context, run_id: Annotated[str, typer.Argument(help="挖掘任务 ID。")]):
    """显示挖掘任务的状态、进度与结果。

    示例：

    \b
      mindex topics runs show tr_0123456789ab
    """
    from mindex.topics.runner import get_run

    print_json(run(_http(lambda: get_run(_store(ctx), run_id))))


@runs_app.command("cancel")
def runs_cancel(ctx: typer.Context, run_id: Annotated[str, typer.Argument(help="挖掘任务 ID。")]):
    """请求取消排队中或运行中的挖掘任务（跨进程生效，在下一个进度检查点停止）。

    示例：

    \b
      mindex topics runs cancel tr_0123456789ab
    """
    from mindex.topics.runner import get_run

    runner = _runs(ctx)
    print_json(run(_http(lambda: (get_run(runner.store, run_id), runner.cancel(run_id))[-1])))


@runs_app.command("retry")
def runs_retry(ctx: typer.Context, run_id: Annotated[str, typer.Argument(help="挖掘任务 ID。")]):
    """在本进程前台重试失败、中断或已取消的挖掘任务。

    示例：

    \b
      mindex topics runs retry tr_0123456789ab
    """
    from mindex.topics.runner import get_run

    runner = _runs(ctx)

    def action():
        get_run(runner.store, run_id)
        lock = runner.row_lock(runner.row(run_id))
        runner.acquire(lock, "重试")
        try:
            row = runner.row(run_id)
            if not runner.retryable(row):
                raise ValueError(runner.not_retryable)
            from mindex.shared.task_progress import reset_task_attempt

            reset_task_attempt(runner.store, runner.table, run_id)
        except BaseException:
            lock.release()
            raise
        runner.run(run_id, ctx.obj, lock)
        return get_run(runner.store, run_id)

    view = run(_http(action))
    print_json(view)
    if view["status"] != "complete":
        raise typer.Exit(1)


@app.command("list")
def list_topics(
    ctx: typer.Context, profile_id: Annotated[str, typer.Argument(help="档案 ID。")],
    status: Annotated[str, typer.Option(help="new（新）/ adopted（已建任务）/ dismissed（已忽略）/ all。")] = "new",
    limit: Annotated[int, typer.Option(min=1, max=500, help="最多条数。")] = 50,
):
    """按总分降序列出选题卡片（含分项分数与覆盖率摘要）。

    示例：

    \b
      mindex topics list tp_0123456789ab --status all
    """
    from mindex.topics import cards

    store = _store(ctx)
    print_json(run(lambda: {"items": cards.list_cards(store, profile_id, status=status, limit=limit),
                            "counts": cards.counts(store, profile_id)}))


@app.command("show")
def show_topic(ctx: typer.Context, card_id: Annotated[str, typer.Argument(help="选题卡片 ID（tc_ 开头）。")]):
    """显示一张选题卡片：来源条目、分项分数、素材覆盖报告（命中片段）、大纲与推荐模板。

    示例：

    \b
      mindex topics show tc_0123456789ab
    """
    from mindex.topics import cards

    print_json(run(lambda: cards.get_card(_store(ctx), card_id)))


def _models(ctx: typer.Context):
    from mindex.models.adapters import make_embedder
    from mindex.topics.llm import make_llm

    return make_llm(ctx.obj), make_embedder(ctx.obj)


@app.command("score")
def score_topic(ctx: typer.Context, card_id: Annotated[str, typer.Argument(help="选题卡片 ID。")]):
    """重新为一张卡片打分（受众匹配调用模型，素材覆盖重新检索当前活动索引），输出新分数与告警。

    示例：

    \b
      mindex topics score tc_0123456789ab
    """
    from mindex.topics import cards

    def action():
        llm, embedder = _models(ctx)
        return cards.rescore(ctx.obj, _store(ctx), card_id, llm=llm, embedder=embedder)

    print_json(run(action))


@app.command("coverage")
def topic_coverage(ctx: typer.Context, card_id: Annotated[str, typer.Argument(help="选题卡片 ID。")]):
    """显示卡片的素材覆盖报告：每条关键画面命中的片段（片段 ID、素材、时间、相似度）与缺口。

    示例：

    \b
      mindex topics coverage tc_0123456789ab
    """
    from mindex.topics import cards

    def action():
        card = cards.get_card(_store(ctx), card_id)
        return {"card_id": card_id, "title": card["title"], "coverage": card["coverage"]}

    print_json(run(action))


@app.command("outline")
def outline_topic(ctx: typer.Context, card_id: Annotated[str, typer.Argument(help="选题卡片 ID。")]):
    """为卡片生成（或重新生成）视频大纲与推荐文案模板，覆盖手工修改过的大纲。

    示例：

    \b
      mindex topics outline tc_0123456789ab
    """
    from mindex.topics import cards
    from mindex.topics.llm import make_llm

    print_json(run(lambda: cards.write_outline(ctx.obj, _store(ctx), card_id, llm=make_llm(ctx.obj))))


@app.command("update")
def update_topic(
    ctx: typer.Context, card_id: Annotated[str, typer.Argument(help="选题卡片 ID。")],
    title: Annotated[str | None, typer.Option(help="新标题。")] = None,
    outline_file: Annotated[Path | None, typer.Option("--outline-file", help="从文件读取大纲（UTF-8 文本）。")] = None,
    template: Annotated[str | None, typer.Option(help="文案模板 ID（见 /studio/templates）。")] = None,
    status: Annotated[str | None, typer.Option(help="new（恢复）或 dismissed（忽略）。")] = None,
):
    """修改卡片的标题、大纲、推荐模板或状态（已建任务的卡片不能改状态）。

    示例：

    \b
      mindex topics update tc_0123456789ab --template history
      mindex topics update tc_0123456789ab --status dismissed
    """
    from mindex.topics import cards

    changes = {"title": title, "template_id": template, "status": status}
    if outline_file is not None:
        try:
            changes["outline"] = outline_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.BadParameter(f"无法读取大纲文件：{exc}", param_hint="--outline-file") from exc
    changes = {k: v for k, v in changes.items() if v is not None}
    if not changes:
        raise typer.BadParameter("至少给出一个要修改的项")
    print_json(run(lambda: cards.update_card(_store(ctx), card_id, cards.CardChanges.model_validate(changes))))


# ---------------------------------------------------------------- 建创作任务与任务档案

job_profile_app = typer.Typer(
    cls=SortedGroup,
    help="从选题创建的创作任务的账号档案快照：show 查看快照与是否已过期，sync 用当前档案更新快照。\n\n"
    "示例：mindex topics job-profile show JOB_ID；mindex topics job-profile sync JOB_ID。",
    no_args_is_help=True,
)
app.add_typer(job_profile_app, name="job-profile")


@app.command("create-job")
def create_job(
    ctx: typer.Context, card_id: Annotated[str, typer.Argument(help="选题卡片 ID。")],
    force: Annotated[bool, typer.Option("--force", help="选题已建过任务时仍再建一份。")] = False,
):
    """把选题建成创作任务：大纲、推荐文案模板、档案默认画幅、来源资料（参考文稿）与账号档案快照都预先设好。

    示例：

    \b
      mindex topics create-job tc_0123456789ab
    """
    from mindex.topics import create_job as topic_jobs

    print_json(run(lambda: topic_jobs.create_from_card(ctx.obj, _store(ctx), card_id, force=force)))


@job_profile_app.command("show")
def job_profile_show(ctx: typer.Context, job_id: Annotated[str, typer.Argument(help="创作任务 ID。")]):
    """显示任务的账号档案快照，以及原档案是否已更新（outdated）。

    示例：

    \b
      mindex topics job-profile show JOB_ID
    """
    from mindex.topics import create_job as topic_jobs

    print_json(run(lambda: topic_jobs.job_profile(ctx.obj, _store(ctx), job_id)))


@job_profile_app.command("sync")
def job_profile_sync(ctx: typer.Context, job_id: Annotated[str, typer.Argument(help="创作任务 ID。")]):
    """用当前档案更新任务的快照；已有文案会提示可重新生成，不会自动重写。

    示例：

    \b
      mindex topics job-profile sync JOB_ID
    """
    from mindex.topics import create_job as topic_jobs

    print_json(run(lambda: topic_jobs.sync_job_profile(ctx.obj, _store(ctx), job_id)))


# ---------------------------------------------------------------- 素材采集清单

gaps_app = typer.Typer(
    cls=SortedGroup,
    help="素材采集清单：选题覆盖报告里没有命中的关键画面，按出现次数排序，指导素材积累。\n\n"
    "示例：mindex topics gaps list tp_0123456789ab；mindex topics gaps export tp_0123456789ab --output gaps.csv。",
    no_args_is_help=True,
)
app.add_typer(gaps_app, name="gaps")
GapStatus = Annotated[str, typer.Option(help="open（待采集）/ done（已采集）/ all。")]


@gaps_app.command("list")
def gaps_list(ctx: typer.Context, profile_id: Annotated[str, typer.Argument(help="档案 ID。")], status: GapStatus = "open"):
    """按出现次数降序列出素材缺口与相关选题。

    示例：

    \b
      mindex topics gaps list tp_0123456789ab --status all
    """
    from mindex.topics import gaps

    print_json({"items": run(lambda: gaps.list_gaps(_store(ctx), profile_id, status=status))})


def _gap_status(ctx: typer.Context, gap_id: str, status: str):
    from mindex.topics import gaps

    print_json(run(lambda: gaps.update_gap(_store(ctx), gap_id, gaps.GapChanges(status=status))))


@gaps_app.command("done")
def gaps_done(ctx: typer.Context, gap_id: Annotated[str, typer.Argument(help="缺口 ID（tg_ 开头）。")]):
    """标记为已采集。

    示例：

    \b
      mindex topics gaps done tg_0123456789ab
    """
    _gap_status(ctx, gap_id, "done")


@gaps_app.command("reopen")
def gaps_reopen(ctx: typer.Context, gap_id: Annotated[str, typer.Argument(help="缺口 ID（tg_ 开头）。")]):
    """重新标记为待采集。

    示例：

    \b
      mindex topics gaps reopen tg_0123456789ab
    """
    _gap_status(ctx, gap_id, "open")


@gaps_app.command("export")
def gaps_export(
    ctx: typer.Context, profile_id: Annotated[str, typer.Argument(help="档案 ID。")], status: GapStatus = "all",
    output: Annotated[Path | None, typer.Option("--output", help="写入文件；省略时输出到标准输出。")] = None,
):
    """导出 CSV（UTF-8 BOM，Excel 可直接打开）：画面、出现次数、状态、相关选题、更新时间。

    示例：

    \b
      mindex topics gaps export tp_0123456789ab --output gaps.csv
    """
    from mindex.topics import gaps

    data = run(lambda: gaps.export_csv(_store(ctx), profile_id, status=status))
    if output is None:
        typer.echo(data.decode("utf-8-sig"), nl=False)
        return
    output.write_bytes(data)
    print_json({"output": str(output), "bytes": len(data)})


# ---------------------------------------------------------------- 定时

schedule_app = typer.Typer(
    cls=SortedGroup,
    help="每日定时挖掘：时刻与条数在档案里设置（profiles update --schedule --schedule-time 08:00）。\n\n"
    "示例：mindex topics schedule status。",
    no_args_is_help=True,
)
app.add_typer(schedule_app, name="schedule")


@schedule_app.command("status")
def schedule_status(ctx: typer.Context):
    """显示各档案的定时设置、下次运行时间、今日运行与产出。

    定时只在 mindex serve（含桌面版后台）运行期间生效；服务启动时会补跑当天错过的定时。

    示例：

    \b
      mindex topics schedule status
    """
    from mindex.topics import scheduler

    enabled = ctx.obj.topics.scheduler.enabled
    value = run(lambda: scheduler.status(_store(ctx), enabled=enabled))
    value["note"] = ("定时只在 mindex serve（含桌面版后台）运行期间生效；服务启动时会补跑当天错过的定时"
                     if enabled else "配置 topics.scheduler.enabled 为 false：mindex serve 不会自动挖掘")
    print_json(value)
