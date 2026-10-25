from __future__ import annotations

import json
import logging
import os
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from typer import rich_utils
from typer.core import TyperGroup

from mindex.cli_agent import app as agent_app
from mindex.cli_agent import mcp_command
from mindex.cli_clips import app as clips_app
from mindex.cli_e2e import app as e2e_app
from mindex.cli_persons import app as persons_app
from mindex.maintenance.backup_cli import app as backup_app
from mindex.shared.cli_support import SortedGroup, print_json
from mindex.shared.config import load_config


class WrappingOptionHighlighter(rich_utils.OptionHighlighter):
    """Keep Typer's colors while folding long Chinese text inside help tables."""

    def __call__(self, text):
        result = super().__call__(text)
        result.overflow = "fold"
        return result


# Rich tables otherwise inherit ellipsis overflow for Chinese parameter descriptions.
rich_utils.highlighter = WrappingOptionHighlighter()


class HelpAwareGroup(SortedGroup):
    """Parse descendant help before Click invokes the configuration callback."""

    def resolve_command(self, ctx, args):
        name, command, remaining = super().resolve_command(ctx, args)
        options = remaining[: remaining.index("--")] if "--" in remaining else remaining
        if (
            any(argument in ctx.help_option_names for argument in options)
            or isinstance(command, TyperGroup)
            and not remaining
            and command.no_args_is_help
        ):
            self._parse_help(command, name, remaining, ctx)
        return name, command, remaining

    @staticmethod
    def _parse_help(command, name, args, parent):
        # Parse options so values like --output --help remain values. Eager help
        # exits here, without invoking configuration or command callbacks.
        with command.make_context(name, list(args), parent=parent) as child:
            if isinstance(command, TyperGroup):
                remaining = [*child._protected_args, *child.args]
                if remaining:
                    name, nested, args = command.resolve_command(child, remaining)
                    HelpAwareGroup._parse_help(nested, name, args, child)


app = typer.Typer(
    cls=HelpAwareGroup,
    rich_markup_mode="rich",
    help=(
        "Mindex 视频素材管理、离线处理、多模态检索与维护。\n\n"
        "首次使用：register/prepare 导入 → enrich-dialogue/analyze 按需补全 → "
        "index --activate 发布 → search 或 serve 使用。单 GPU 按阶段轮换服务。\n\n"
        "全局选项必须放在子命令前。示例：mindex --config config.local.yaml clean。"
        "普通命令合并设置页运行配置；e2e、clips、backup 使用 YAML 原值。\n\n"
        "每一级均支持 -h / --help，例如 mindex clean -h、mindex backup import --help。"
        "帮助不加载配置或访问素材库。详细机制见 docs/cli-reference.md。"
    ),
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
)
app.add_typer(backup_app, name="backup")
indexes = typer.Typer(
    cls=HelpAwareGroup,
    help="管理内容索引版本：list 查看版本，activate 检查后发布或回退。\n\n"
    "新建索引用 mindex index，一致性检查用 mindex check。回退前恢复对应模型与配置。\n\n"
    "示例：mindex indexes list；mindex indexes activate INDEX_ID。",
    no_args_is_help=True,
)
app.add_typer(indexes, name="indexes")
app.add_typer(e2e_app, name="e2e")
app.add_typer(persons_app, name="persons")
app.add_typer(clips_app, name="clips")
app.add_typer(agent_app, name="agent")
app.command("mcp")(mcp_command)


class IndexMode(str, Enum):
    auto = "auto"
    visual = "visual"
    enriched = "enriched"


@app.callback()
def configure(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="配置 YAML 路径，优先于 MINDEX_CONFIG；默认 config.local.yaml。必须放在子命令之前。",
        ),
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="覆盖数据目录，按当前工作目录解析；必须放在子命令之前。")
    ] = None,
    verbose: Annotated[bool, typer.Option(help="启用 DEBUG 日志；默认 INFO。")] = False,
    no_runtime_overrides: Annotated[
        bool,
        typer.Option(
            "--no-runtime-overrides",
            help="只使用 YAML，不合并设置页保存的模型档案与业务覆盖；e2e/clips/backup 本就不合并。",
        ),
    ] = False,
):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(message)s")
    selected_config = Path(config or os.environ.get("MINDEX_CONFIG", "config.local.yaml")).resolve()
    ctx.meta["config_path"] = selected_config
    if ctx.invoked_subcommand == "backup" and not selected_config.exists():
        from mindex.shared.config import Config

        ctx.obj = Config(data_dir=selected_config.parent / "data")
    else:
        ctx.obj = load_config(config)
    if data_dir is not None:
        ctx.obj.data_dir = data_dir.resolve()
    if no_runtime_overrides:
        ctx.obj.runtime_overrides = False
    # 设置页保存的覆盖层对 CLI 同样生效（Web 与 CLI 行为一致）；e2e 验证框架保持 yaml 原值。
    if ctx.invoked_subcommand not in {"e2e", "clips", "backup"}:
        from mindex.settings.runtime_settings import apply_runtime_overrides

        ctx.obj = apply_runtime_overrides(ctx.obj)


@app.command()
def register(
    ctx: typer.Context,
    videos: Annotated[
        list[Path], typer.Argument(help="本地原视频文件，可连续传入多个路径；含空格的路径请加引号。")
    ],
):
    """验证原片、按内容 SHA256 去重并登记，不进行分镜或模型处理。

    可一次输入多个文件，原片复制到数据目录；每个文件输出 source_id、duplicate 的 JSON。后续用 prepare 执行分镜。

    示例：

    \b
      mindex register /路径/电影.mp4 /路径/素材.mkv
    """
    from mindex.processing.pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    for path in videos:
        print_json(pipeline.register(path))


@app.command()
def prepare(
    ctx: typer.Context,
    videos: Annotated[
        list[Path], typer.Argument(help="本地原视频文件，可连续传入多个路径；含空格的路径请加引号。")
    ],
):
    """导入原视频、检测分镜并生成镜头、精细片段和缩略图。

    相当于 detect 后 split，输出素材 ID。默认 adaptive 本地检测无需模型；配置 transnet 时需对应检测服务。已有计划与切片可复用，失败后用相同配置重跑。

    示例：

    \b
      mindex prepare /路径/电影.mp4
    """
    from mindex.processing.pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock():
        for path in videos:
            typer.echo(pipeline.prepare(path))


@app.command()
def detect(ctx: typer.Context, video: Annotated[Path, typer.Argument(help="本地原视频文件路径。")]):
    """登记原片并保存分镜计划，不生成切片或调用嵌入服务。

    计划写入 plans/SOURCE_ID.json，输出素材 ID。默认使用本地 adaptive；transnet 后端需要检测服务。随后执行 split；改变切分布局须使用新数据目录。

    示例：

    \b
      mindex detect /路径/电影.mp4
    """
    from mindex.processing.pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock():
        typer.echo(pipeline.detect(video))


@app.command()
def split(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="已保存分镜计划的素材 ID，来自 detect 输出。")],
):
    """按照已保存的分镜计划生成媒体与缩略图。

    SOURCE 是 detect 输出的素材 ID，不是文件路径。重跑复用可用文件，不重新检测整片；切分配置须与登记时一致。完成后输出素材 ID。

    示例：

    \b
      mindex split SOURCE_ID
    """
    from mindex.processing.pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock(source, "shots"):
        pipeline.split(source)
    typer.echo(source)


@app.command()
def inspect(ctx: typer.Context, source: Annotated[str, typer.Argument(help="要查看的素材 ID。")]):
    """查看一个素材的分镜、时长分布、切片进度和缺失媒体。

    输出 JSON 报告及最近处理记录，不执行分镜或模型推理。SOURCE 使用 register/detect/prepare 返回的素材 ID。

    示例：

    \b
      mindex inspect SOURCE_ID
    """
    from mindex.diagnostics.film import source_report

    print_json(source_report(ctx.obj, source))


@app.command("export")
def export_source(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="要导出分镜报告的素材 ID。")],
    output: Annotated[Path | None, typer.Option(help="报告目录；默认 data_dir/reports/SOURCE_ID/。")] = None,
):
    """导出素材的 JSON、CSV 与可在浏览器打开的分镜检查页。

    默认写入 data_dir/reports/SOURCE_ID/；包括 manifest.json、shots.csv、segments.csv、review.html。报告依赖媒体相对路径，移动时保留布局；这不是可恢复的数据备份，备份请用 backup export。

    示例：

    \b
      mindex export SOURCE_ID --output /路径/检查报告
    """
    from mindex.diagnostics.film import export_source as run_export
    from mindex.processing.pipeline import Pipeline

    with Pipeline(ctx.obj).lock():
        print_json(run_export(ctx.obj, source, output))


@app.command()
def transcribe(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Option(help="素材 ID；省略处理全部素材，不是文件路径。")] = None,
):
    """使用配置的 ASR 转写已完成分镜的素材，保留断点检查点。

    省略 --source 处理全部素材。mindex 后端保存带时间戳结果；llama_cpp/openai 后端保存原始文本后需 align；minimax 走对白管线保存服务时间戳。无音轨或禁用 ASR 会记录相应状态，不执行 LLM 视觉分析。

    示例：

    \b
      mindex transcribe --source SOURCE_ID
    """
    from mindex.processing.pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock(source, "dialogue"):
        pipeline.transcribe(source)


@app.command("enrich-dialogue")
def enrich_dialogue(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Argument(help="可选素材 ID，省略批量处理已登记素材。")] = None,
    subtitle_stream: Annotated[
        int | None,
        typer.Option(min=0, help="ffprobe 的全局文本字幕流索引（非字幕序号）；method=subtitle 时必填。"),
    ] = None,
    method: Annotated[
        str, typer.Option(help="auto 字幕优先并允许回退 ASR；subtitle 严格选轨；asr 直接转写。")
    ] = "auto",
    no_publish: Annotated[
        bool, typer.Option("--no-publish", help="只补充对白，不构建或发布索引；适合分阶段轮换 GPU 服务。")
    ] = False,
    workers: Annotated[int, typer.Option(min=1, max=8, help="对白并发工作数，范围 1–8。")] = 2,
):
    """补充字幕或 ASR 对白，支持断点续跑，默认检查并发布索引。

    省略 SOURCE 批量处理。auto 优先可用文本字幕，否则 ASR；subtitle 必须指定 --subtitle-stream，失败不回退；asr 直接转写。发布需要嵌入服务，可用 --no-publish 将处理与发布分开。输出进度及 JSON 结果；不触发 LLM 分析。

    示例：

    \b
      mindex enrich-dialogue SOURCE_ID --method auto --no-publish
    """
    from mindex.admin.dialogue_tasks import execute_dialogue
    from mindex.processing.pipeline import Pipeline

    with Pipeline(ctx.obj).lock(source, "dialogue"):
        print_json(
            execute_dialogue(
                ctx.obj,
                source,
                method=method,
                subtitle_stream=subtitle_stream,
                publish=not no_publish,
                workers=workers,
                progress=lambda value: typer.echo(json.dumps(value, ensure_ascii=False)),
            )
        )


@app.command()
def align(
    ctx: typer.Context,
    python: Annotated[
        Path, typer.Argument(help="独立 CPU 对齐环境的 Python 解释器，例如 .venv-align/bin/python。")
    ],
    source: Annotated[str | None, typer.Option(help="素材 ID；省略处理全部素材，不是文件路径。")] = None,
):
    """用独立 CPU ForcedAligner 环境对齐缓存的原始 ASR 文本。

    PYTHON 是独立环境解释器的位置参数，不是 --python 选项。用于 llama_cpp/openai 等纯文本转写路径；固定 asr.aligner_revision，不能将对齐依赖装进应用环境。省略 --source 处理全部素材。

    示例：

    \b
      mindex align .venv-align/bin/python --source SOURCE_ID
    """
    from mindex.processing.pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock(source, "dialogue"):
        pipeline.align(python, source)


@app.command()
def analyze(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Option(help="素材 ID；省略处理全部素材，不是文件路径。")] = None,
):
    """分析镜头和精细片段，生成动作、场景、情绪等描述及连续场景。

    需要已完成分镜的素材和配置的视觉理解服务。省略 --source 处理全部素材；检查点绑定分析配置，失败后可重跑。分析结果不会自动改写已发布索引，随后运行 index。

    示例：

    \b
      mindex analyze --source SOURCE_ID
    """
    from mindex.processing.pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock(source, "analysis"):
        pipeline.analyze(source)


@app.command(name="index")
def build_index(
    ctx: typer.Context,
    activate: Annotated[bool, typer.Option(help="检查通过后设为活动索引；缺省仅构建。")] = False,
    mode: Annotated[
        IndexMode, typer.Option(help="auto 按可用渠道建库；visual 画面入口；enriched 旧版完整索引兼容模式。")
    ] = IndexMode.auto,
    channel: Annotated[
        list[str] | None,
        typer.Option(
            help="可重复指定 visual/dialogue/analysis；默认 auto 选全部渠道、visual 选画面；enriched 不支持。"
        ),
    ] = None,
    source: Annotated[
        str | None, typer.Option(help="仅更新指定素材；省略处理全部。新版保留活动索引中的其他素材。")
    ] = None,
    kind: Annotated[
        list[str] | None,
        typer.Option(
            help="可重复指定 moment/shot/scene；默认 auto 三种，visual 为 moment/shot 且不能选 scene。"
        ),
    ] = None,
):
    """建立或继续版本化索引，输出索引 ID；默认不发布。

    auto 按已有画面、对白、分析分别建库，不运行 ASR 或 LLM；visual 默认只更新画面；enriched 是仍保留的旧版完整索引兼容入口，不支持 --channel。所选向量渠道需要匹配的嵌入服务。新版按素材/渠道增量更新并保留其他已有成员；--activate 在检查通过后发布。

    示例：

    \b
      mindex index --mode visual --source SOURCE_ID --kind moment --kind shot --activate
    """
    from mindex.processing.indexing import IndexBuilder
    from mindex.processing.pipeline import Pipeline

    with Pipeline(ctx.obj).lock(source, "publish"):
        typer.echo(
            IndexBuilder(ctx.obj).build(
                activate=activate,
                mode=mode.value,
                source_id=source,
                kinds=tuple(kind) if kind else None,
                channels=channel,
            )
        )


@app.command()
def ingest(
    ctx: typer.Context,
    videos: Annotated[
        list[Path], typer.Argument(help="本地原视频文件，可连续传入多个路径；含空格的路径请加引号。")
    ],
    activate: Annotated[bool, typer.Option(help="检查通过后设为活动索引；缺省仅构建。")] = False,
):
    """依次导入、分镜、补充对白并建立 auto 索引。

    可输入多个原片；对白阶段面向库内素材，不仅限本次输入。不执行 LLM 视觉分析，默认不激活索引。需检测、ASR（无字幕时）与嵌入能力在流程中可访问；单 GPU 手动轮换模型时请分步 prepare、enrich-dialogue、index。

    示例：

    \b
      mindex ingest /路径/电影.mp4 --activate
    """
    from mindex.processing.indexing import IndexBuilder
    from mindex.processing.pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock():
        for path in videos:
            pipeline.prepare(path)
        pipeline.enrich_dialogue()
        typer.echo(IndexBuilder(ctx.obj).build(activate=activate))


@app.command()
def search(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument(help="文字查询内容；含空格的描述请加引号。")],
    granularity: Annotated[
        str, typer.Option(help="检索粒度 moment（精细片段）、shot（镜头）、scene（已分析场景），需已建库。")
    ] = "moment",
    limit: Annotated[int, typer.Option(help="返回结果数量，范围 1–50。")] = 20,
    channel: Annotated[
        list[str] | None,
        typer.Option(help="可重复选择 visual/dialogue/analysis；省略使用全部可用渠道，新版索引生效。"),
    ] = None,
):
    """用中文文字检索已发布素材，输出 JSON 排名结果。

    需要活动索引且包含所选粒度。新版支持 visual/dialogue/analysis 渠道；文本渠道在嵌入不可用时可显式降级 BM25，画面召回需要匹配的嵌入服务。旧格式索引沿用旧融合路径。分数是排序依据，不是概率；图片/视频查询使用 Web 或 HTTP API。

    示例：

    \b
      mindex search '雨夜街道上奔跑的人' --granularity shot --limit 10
    """
    from mindex.search.search import SearchEngine
    from mindex.shared.types import TextSearch

    print_json(
        SearchEngine(ctx.obj).text(
            TextSearch(text=text, granularity=granularity, limit=limit, channels=channel)
        )
    )


@app.command()
def status(
    ctx: typer.Context,
    health: Annotated[bool, typer.Option(help="额外请求嵌入健康状态并检查与活动索引的空间匹配。")] = False,
):
    """查看活动索引、素材、处理记录及模型配置状态。

    输出 JSON；默认不做额外的嵌入健康探测，--health 请求嵌入服务并检查空间与活动索引是否匹配。不会启动模型或构建索引。

    示例：

    \b
      mindex status --health
    """
    from mindex.search.search import status as get_status

    print_json(get_status(ctx.obj, health=health))


@app.command()
def check(
    ctx: typer.Context,
    index_id: Annotated[str | None, typer.Option(help="内容索引版本 ID；省略使用活动索引。")] = None,
):
    """检查索引成员、表示、向量和媒体引用的一致性。

    默认检查活动索引，也可指定未激活索引 ID。输出 index_id、ok、issues 的 JSON；发现问题退出码为 1。不会自动修复、激活或重新计算向量，也不替代真实模型质量评测。

    示例：

    \b
      mindex check --index-id INDEX_ID
    """
    from mindex.processing.indexing import check_index
    from mindex.shared.storage import Store

    store = Store(ctx.obj.data_dir)
    active = store.active_index()
    selected = index_id or (active["id"] if active else None)
    if not selected:
        raise typer.BadParameter("请指定索引 ID，或先发布索引")
    issues = check_index(store, selected)
    print_json({"index_id": selected, "ok": not issues, "issues": issues})
    if issues:
        raise typer.Exit(1)


@indexes.command("list")
def list_indexes(ctx: typer.Context):
    """列出所有索引版本及当前活动版本。

    输出 active 与 versions 的 JSON，版本按创建时间倒序。无需模型；查看 ID 后可用 check 检查、indexes activate 切换。

    示例：

    \b
      mindex indexes list
    """
    from mindex.shared.storage import Store

    store = Store(ctx.obj.data_dir)
    print_json(
        {
            "active": store.active_index(),
            "versions": store.rows("SELECT * FROM index_versions ORDER BY created_at DESC"),
        }
    )


@indexes.command("activate")
def activate_index(
    ctx: typer.Context, index_id: Annotated[str, typer.Argument(help="内容索引版本 ID；省略使用活动索引。")]
):
    """检查并激活指定内容索引，可用于发布或回退。

    通过一致性检查后在 SQLite 中切换活动版本；schema 2 空间还会校验当前嵌入身份与能力门槛。回退前先恢复该版本对应的模型服务及配置，不会转换已有向量。

    示例：

    \b
      mindex indexes activate INDEX_ID
    """
    from mindex.processing.indexing import check_index
    from mindex.processing.pipeline import Pipeline
    from mindex.shared.storage import Store

    store = Store(ctx.obj.data_dir)
    with Pipeline(ctx.obj).lock():
        issues = check_index(store, index_id)
        if issues:
            raise typer.BadParameter("索引检查失败：" + "; ".join(issues[:5]))
        version = store.index_version(index_id)
        if version["spec"]["space"].get("schema") == 2:
            from mindex.models.adapters import make_embedder

            client = make_embedder(ctx.obj)
            if client.identity() != version["spec"]["space"]:
                raise typer.BadParameter("当前模型空间与待发布索引不同")
            client.require_gate()
        store.activate(index_id)
    typer.echo(f"已发布索引 {index_id}；检索模型须使用该版本对应的配置")


@app.command()
def clean(
    ctx: typer.Context,
    apply: Annotated[
        bool, typer.Option("--apply", help="实际删除扫描到的可回收数据；缺省只预览 JSON，不删除候选。")
    ] = False,
):
    """扫描磁盘可回收项并输出 JSON 报告；默认仅预览。

    加 --apply 才实际删除。保留数量与过期天数在 YAML 的 clean 节配置，
    CLI 不提供按类别删除或修改过期阈值的选项。

    A 级：临时残留、孤儿媒体/参考图/音色、人物冗余展示帧；
    部分上传与未注册原片残留受 clean.temp_grace_hours 宽限期保护。

    B 级：按策略清理旧索引、旧指纹检查点、媒体缓存、报告、运行记录，
    以及 Studio 预览、旧 attempt 和导出 ZIP。
    保留活动索引、全部非 ready 索引和最近 clean.index_keep_versions 个非活动 ready 版本。
    删除旧索引后失去该版本的回退与向量复用能力。

    C 级：模型缓存、e2e/验证产物与数据库备份只在 notes 报告，不自动删除，也不执行 VACUUM。

    输出 JSON：categories 为逐类别结果，total 为候选数量/字节数，notes 为保留项建议。
    请检查类别的 error/skipped，单类别错误不一定使整体退出码为 1。
    管线或索引锁冲突退出 1；Studio 锁冲突只跳过该部分。大规模清理前建议停服。

    示例：

    \b
      mindex clean
      mindex clean --apply
      mindex --config /路径/config.yaml --data-dir /路径/data clean
    """
    from mindex.maintenance.clean import CleanBusy, clean_library

    try:
        print_json(clean_library(ctx.obj, apply=apply))
    except CleanBusy as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None


def _is_loopback_host(host: str) -> bool:
    """127.0.0.0/8、::1 与 localhost 视为仅本机可访问。"""
    import ipaddress

    value = host.strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


@app.command()
def serve(
    ctx: typer.Context,
    host: Annotated[
        str, typer.Option(help="监听地址；127.0.0.1 仅本机，0.0.0.0 监听所有接口。")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTP 监听端口。")] = 8000,
    index_id: Annotated[
        str | None, typer.Option(help="将查询与相关媒体固定到指定 ready 索引，不修改活动索引。")
    ] = None,
    allow_insecure: Annotated[
        bool,
        typer.Option(
            "--allow-insecure",
            help="允许在未设置 MINDEX_WEB_TOKEN 时监听非回环地址；网络中的任何人都可读写全部功能。",
        ),
    ] = False,
):
    """启动同一进程提供的网页、静态资源和 HTTP API。

    页面为 /、/admin、/persons、/studio、/settings，接口文档 /docs。不启动模型服务。--index-id 将检索和相关媒体固定到指定 ready 索引，不更改活动指针。监听非回环地址时必须设置 MINDEX_WEB_TOKEN，或显式传 --allow-insecure。无需前端构建；用 Ctrl+C 停止服务。

    示例：

    \b
      mindex serve --host 127.0.0.1 --port 8000
      MINDEX_WEB_TOKEN=<令牌> mindex serve --host 0.0.0.0
    """
    import uvicorn

    from mindex.api import create_app
    from mindex.shared.web_auth import configured_token

    if not _is_loopback_host(host) and not configured_token():
        if not allow_insecure:
            typer.secho(
                f"拒绝启动：监听 {host} 但未设置 MINDEX_WEB_TOKEN，网络中的任何人都可读写全部功能。"
                "请设置环境变量 MINDEX_WEB_TOKEN 启用访问令牌，或确认风险后传入 --allow-insecure。",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)
        typer.secho(
            f"警告：监听 {host} 且未设置 MINDEX_WEB_TOKEN，网络中的任何人都可读写全部功能。",
            fg=typer.colors.YELLOW,
            err=True,
        )
    from mindex.search.warmup import warm_up_in_background

    app = create_app(ctx.obj, index_id=index_id)
    warm_up_in_background(ctx.obj, index_id)  # 后台线程，不阻塞监听；失败只记日志
    uvicorn.run(app, host=host, port=port)


@app.command()
def evaluate(
    ctx: typer.Context,
    queries: Annotated[Path, typer.Argument(help="查询标注 JSONL 文件路径。")],
    split: Annotated[
        str, typer.Option(help="查询文件的 split 分组名，例如 dev、test、video；仅评测匹配条目。")
    ] = "test",
    output: Annotated[
        Path | None, typer.Option(help="完整评测报告 JSON 路径；终端始终输出指标摘要。")
    ] = None,
    index_id: Annotated[str | None, typer.Option(help="内容索引版本 ID；省略使用活动索引。")] = None,
):
    """用人工标注的 JSONL 查询集评测检索质量。

    仅评测 --split 对应分组，拒绝显式标为未确认的草稿；需 relevant 或 no_answer 标注。使用活动索引或 --index-id，要求相应查询服务就绪。终端输出各检索方案的指标摘要；完整详情可写入 --output。工程测试与模型质量评测分开。

    示例：

    \b
      mindex evaluate eval/queries.jsonl --split test --output /路径/eval.json
    """
    from mindex.diagnostics.evaluation import evaluate as run_evaluation

    report = run_evaluation(ctx.obj, queries, split, output, index_id=index_id)
    print_json({k: {a: b for a, b in v.items() if a != "details"} for k, v in report["profiles"].items()})


@app.command("model-check")
def check_model(
    ctx: typer.Context,
    video: Annotated[
        Path, typer.Argument(help="真实短视频，时长不超过 video.short_shot_seconds（默认 6 秒）。")
    ],
    text: Annotated[str, typer.Option(help="与短视频比较的查询描述。")] = "一个人在房间里走动",
    repeats: Annotated[
        int, typer.Option(min=1, max=20, help="重复测量次数，范围 1–20；首轮与后续耗时分别记录。")
    ] = 3,
    output: Annotated[Path | None, typer.Option(help="额外保存完整联调 JSON；省略仅输出到终端。")] = None,
):
    """对真实短视频和文字执行嵌入联调与重复测量。

    视频长度不超过配置 video.short_shot_seconds（默认 6 秒）。需可用嵌入服务；输出空间身份、维度、范数、余弦、耗时和可用的 /metrics 信息，可另存 JSON。不会证明真实检索质量或自动发布索引。

    示例：

    \b
      mindex model-check /路径/短视频.mp4 --text '画面描述' --repeats 3
    """
    from mindex.diagnostics.benchmark import model_check

    report = model_check(ctx.obj, video, text, repeats)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_json(report)


if __name__ == "__main__":
    app()
