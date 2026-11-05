from __future__ import annotations

import json
import logging
import os
import time
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
from mindex.cli_studio import app as studio_app
from mindex.cli_topics import app as topics_app
from mindex.cli_uploads import app as uploads_app
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
        if any(argument in ctx.help_option_names for argument in options) or self._empty_group(command, remaining):
            self._parse_help(command, name, remaining, ctx)
        return name, command, remaining

    @staticmethod
    def _empty_group(command, args):
        """参数只由子命令名组成并停在一个无参即帮助的命令组（如 ``topics profiles``）。"""
        while isinstance(command, TyperGroup) and args and args[0] in command.commands:
            command, args = command.commands[args[0]], args[1:]
        return isinstance(command, TyperGroup) and not args and command.no_args_is_help

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
app.add_typer(topics_app, name="topics")
app.add_typer(studio_app, name="studio")
app.add_typer(uploads_app, name="uploads")
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
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S%z")
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
    if ctx.invoked_subcommand not in {"e2e", "clips", "backup", "migrate", "migration", "doctor", "mcp"}:
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
    force: Annotated[bool, typer.Option("--force", help="强制重新转写，忽略已完成结果和转写检查点。")] = False,
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
        pipeline.transcribe(source, **({"force": True} if force else {}))


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
    force: Annotated[bool, typer.Option("--force", help="强制重新提取对白，忽略已完成结果和转写检查点。")] = False,
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
                **({"force": True} if force else {}),
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
    force: Annotated[bool, typer.Option("--force", help="强制重新分析已完成的片段并重新组织场景。")] = False,
):
    """分析镜头和精细片段，生成动作、场景、情绪等描述及连续场景。

    需要已完成分镜的素材和配置的视觉理解服务。省略 --source 处理全部素材；默认跳过已完成的片段（包括切换模型后），失败后可续跑；只有显式 --force 才重新分析。分析结果不会自动改写已发布索引，随后运行 index。

    示例：

    \b
      mindex analyze --source SOURCE_ID
    """
    from mindex.processing.pipeline import Pipeline
    from mindex.shared.command_progress import CommandProgress

    with CommandProgress("LLM 分析") as progress:
        pipeline = Pipeline(ctx.obj)
        logging.getLogger(__name__).info("分析模型：%s / %s；范围：%s", ctx.obj.analyzer.backend, ctx.obj.analyzer.model, source or "全部素材")
        progress.update({"stage": "lock_wait"})
        with pipeline.lock(source, "analysis"):
            progress.update({"stage": "analyze", "message": "扫描素材并准备分析"})
            pipeline.analyze(source, progress=progress.update, force=force)


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
    from mindex.shared.command_progress import CommandProgress

    with CommandProgress("索引更新") as progress:
        logging.getLogger(__name__).info("嵌入模型：%s / %s；范围：%s；渠道：%s；粒度：%s；发布：%s",
                                         ctx.obj.embedding.backend, ctx.obj.embedding.model, source or "全部素材",
                                         ",".join(channel or (["visual"] if mode == IndexMode.visual else ["visual", "dialogue", "analysis"])),
                                         ",".join(kind or (["moment", "shot"] if mode == IndexMode.visual else ["moment", "shot", "scene"])), activate)
        progress.update({"stage": "lock_wait"})
        from contextlib import nullcontext

        # 持久任务由 worker 拿处理锁，调用端等待期间不能持有同一把锁。
        with (nullcontext() if ctx.obj.queue.backend == "postgres" else Pipeline(ctx.obj).lock(source, "publish")):
            progress.update({"stage": "index_prepare", "message": "初始化索引构建器"})
            result = IndexBuilder(ctx.obj).build(
                activate=activate, mode=mode.value, source_id=source,
                kinds=tuple(kind) if kind else None, channels=channel, progress=progress.update,
            )
        typer.echo(result)


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
def migrate(ctx: typer.Context):
    """把配置选定的元数据库升级到当前 schema（集群形态的唯一 DDL 入口）。

    local 形态沿用 SQLite 构造即迁移；pg-single/cluster 形态下 API 与 worker 启动只校验版本、
    不执行 DDL，必须先运行本命令（plans §4：避免多个版本同时改表）。

    示例：

    \b
      MINDEX_DATABASE_DSN=postgres://… mindex migrate
    """
    import os

    config = ctx.obj
    if config.database.backend == "postgres":
        section = config.database.postgres
        dsn = os.environ.get(section.dsn_env, "").strip()
        if not dsn:
            raise typer.BadParameter(f"缺少环境变量 {section.dsn_env}（PostgreSQL DSN）")
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指引
            raise typer.BadParameter("PostgreSQL 后端需要 psycopg：uv sync --extra cluster") from exc
        from mindex.shared.db.migrate_pg import migrate_pg

        with psycopg.connect(dsn) as conn:
            version = migrate_pg(conn)
        print_json({"backend": "postgres", "schema_version": version})
    else:
        from mindex.shared.storage import Store

        Store.open(config)
        print_json({"backend": "sqlite", "schema_version": "latest"})


migration = typer.Typer(
    cls=HelpAwareGroup,
    help="local↔cluster 数据迁移（plans §10；仅同版本程序，先停写）。\n\n"
    "preflight 校验；export 停写导出；load 空库装载/续跑；verify 对账；activate 记录切换边界。\n\n"
    "示例：mindex migration export --target /backup/run1。",
    no_args_is_help=True,
)
app.add_typer(migration, name="migration")


@migration.command("export")
def migration_export(
    ctx: typer.Context,
    target: Annotated[Path, typer.Option(help="新建空目录，输出带摘要的迁移 bundle。")],
    with_media: Annotated[bool, typer.Option("--with-media", help="复制全部引用媒体与权威工作文件。")] = False,
    with_vectors: Annotated[bool, typer.Option("--with-vectors", help="按原 ID/embedding 导出全部 ready 集合。")] = False,
    legacy_namespace: Annotated[bool, typer.Option("--legacy-namespace", help="显式读取旧版未按库 UUID 隔离的共享资源，仅用于迁移导出。")] = False,
):
    """停写导出 local/PG 业务库、媒体与向量；自动裁剪秘密。

    示例：

    \b
      mindex migration export --target /backup/run1 --with-media --with-vectors
    """
    from mindex.maintenance.migration import export_bundle

    try:
        print_json(export_bundle(ctx.obj, target, with_media=with_media, with_vectors=with_vectors,
                                 legacy_namespace=legacy_namespace))
    except (ValueError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@migration.command("preflight")
def migration_preflight(
    ctx: typer.Context,
    source: Annotated[Path, typer.Option(help="迁移 bundle 目录。")],
):
    """校验 bundle、目标空库与同 migration_id 续跑条件，不写业务数据。

    示例：

    \b
      mindex migration preflight --source /backup/run1
    """
    from mindex.maintenance.migration import preflight

    try:
        print_json(preflight(ctx.obj, source))
    except (ValueError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@migration.command("load")
def migration_load(
    ctx: typer.Context,
    source: Annotated[Path, typer.Option(help="带 manifest.json 的迁移 bundle 目录。")],
    with_vectors: Annotated[bool, typer.Option("--with-vectors", help="装载清单中的向量集合，不重新嵌入。")] = False,
):
    """装载到空目标或续跑同 migration_id；非空目标及异值冲突一律拒绝。

    PostgreSQL 目标先运行 mindex migrate；不会删除 schema。媒体自动随清单装载。

    示例：

    \b
      mindex migration load --source /backup/run1 --with-vectors
    """
    from mindex.maintenance.migration import load_bundle

    try:
        print_json(load_bundle(ctx.obj, source, with_vectors=with_vectors))
    except (ValueError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@migration.command("verify")
def migration_verify(
    ctx: typer.Context,
    source: Annotated[Path, typer.Option(help="迁移 bundle 目录，与当前目标逐项对账。")],
):
    """核对元数据、媒体摘要和向量内容；存在差异以退出码 1 返回。

    示例：

    \b
      mindex migration verify --source /backup/run1
    """
    from mindex.maintenance.migration import verify_bundle

    try:
        result = verify_bundle(ctx.obj, source)
    except (ValueError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_json(result)
    if not result["consistent"]:
        raise typer.Exit(1)


@migration.command("activate")
def migration_activate(
    ctx: typer.Context,
    source: Annotated[Path, typer.Option(help="已装载的迁移 bundle 目录。")],
):
    """重新全量对账并记录激活边界，之后禁止重放旧 bundle。

    激活记录不会修改流量路由；停掉源写入并按部署 runbook 切换服务。

    示例：

    \b
      mindex migration activate --source /backup/run1
    """
    from mindex.maintenance.migration import activate_bundle

    try:
        print_json(activate_bundle(ctx.obj, source))
    except (ValueError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc


def _pg_dsn(config) -> str:
    import os

    dsn = os.environ.get(config.database.postgres.dsn_env, "").strip()
    if not dsn:
        raise typer.BadParameter(f"缺少环境变量 {config.database.postgres.dsn_env}（PostgreSQL DSN）")
    return dsn


@app.command()
def doctor(ctx: typer.Context):
    """部署诊断：后端组合校验与逐项连通检查（plans §11；启动前运行）。

    输出 JSON（checks/summary），全部通过退出码 0，否则 1。只读检查，不执行 DDL、不触碰数据。

    示例：

    \b
      MINDEX_DATABASE_DSN=postgres://… mindex doctor
    """
    from mindex.doctor import doctor

    report = doctor(ctx.obj)
    print_json(report)
    if not report["ok"]:
        raise typer.Exit(1)


@app.command()
def worker(
    ctx: typer.Context,
    queues: Annotated[str, typer.Option(help="消费的任务类别（逗号分隔：source,dialogue,person,topic）。")] = "source,dialogue,person,topic",
    concurrency: Annotated[int, typer.Option(min=1, help="并行执行槽位；起步建议 1，容量验证后增加。")] = 1,
    once: Annotated[bool, typer.Option(help="领取并执行一个任务后退出（验证/测试用）。")] = False,
    idle_exit: Annotated[float | None, typer.Option(help="空闲指定秒数后退出（测试用）。")] = None,
):
    """启动 worker 消费持久队列（plans §7.2；需要 queue.backend=postgres）。

    至少一次交付：claim 带心跳续期与 fencing token，暂停超过 TTL 的旧执行不得提交；
    取消意图在执行间轮询传播。起步用小并发，完成容量与 GPU 准入测试后增加。

    示例：

    \b
      MINDEX_DATABASE_DSN=postgres://… mindex worker --queues source,dialogue --concurrency 2
    """
    import logging as _logging

    from mindex.worker_runtime import Worker

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        runner = Worker(ctx.obj, queues=[q.strip() for q in queues.split(",") if q.strip()],
                        concurrency=concurrency, once=once, idle_exit=idle_exit)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    executed = runner.run()
    print_json({"worker": runner.identity, "executed": executed})


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

    store = Store.open(ctx.obj)
    active = store.active_index()
    selected = index_id or (active["id"] if active else None)
    if not selected:
        raise typer.BadParameter("请指定索引 ID，或先发布索引")
    issues = check_index(store, selected, config=ctx.obj)
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

    store = Store.open(ctx.obj)
    print_json(
        {
            "active": store.active_index(),
            "versions": store.rows("SELECT * FROM index_versions ORDER BY created_at DESC"),
        }
    )


index_tasks_app = typer.Typer(cls=HelpAwareGroup, help="查询、重试或取消持久索引任务。\n\n"
    "示例：mindex indexes tasks list；mindex indexes tasks show TASK_ID。", no_args_is_help=True)
indexes.add_typer(index_tasks_app, name="tasks")


def _index_tasks(config):
    from mindex.admin.source_tasks import IndexTasks
    from mindex.settings.runtime_settings import RuntimeConfig
    from mindex.shared.storage import Store

    store = Store.open(config)
    return IndexTasks(RuntimeConfig(config, store), store)


@index_tasks_app.command("list")
def list_index_tasks(ctx: typer.Context, limit: Annotated[int, typer.Option(min=1, max=200, help="最多返回的任务数。")] = 50):
    """列出最近的索引任务。\n\n示例：mindex indexes tasks list --limit 20。"""
    from mindex.shared.background_tasks import public_task

    runner = _index_tasks(ctx.obj)
    print_json({"items": [public_task(row) for row in runner.store.rows(
        "SELECT * FROM index_tasks ORDER BY created_at DESC,rowid DESC LIMIT ?", (limit,))]})


@index_tasks_app.command("show")
def show_index_task(ctx: typer.Context, task_id: Annotated[str, typer.Argument(help="索引任务 ID。")]):
    """查询索引任务进度与结果。\n\n示例：mindex indexes tasks show TASK_ID。"""
    print_json(_index_tasks(ctx.obj).get(task_id))


@index_tasks_app.command("retry")
def retry_index_task(ctx: typer.Context, task_id: Annotated[str, typer.Argument(help="索引任务 ID。")]):
    """重试失败、中断或已取消的索引任务。\n\n示例：mindex indexes tasks retry TASK_ID。"""
    print_json(_index_tasks(ctx.obj).retry(task_id))


@index_tasks_app.command("cancel")
def cancel_index_task(ctx: typer.Context, task_id: Annotated[str, typer.Argument(help="索引任务 ID。")]):
    """取消索引任务。\n\n示例：mindex indexes tasks cancel TASK_ID。"""
    print_json(_index_tasks(ctx.obj).cancel(task_id))


@indexes.command("lexical")
def lexical(
    ctx: typer.Context,
    index_id: Annotated[str | None, typer.Option(help="目标索引版本 ID；省略重建全部 ready 版本。")] = None,
    replace: Annotated[bool, typer.Option("--replace", help="已存在也强制重建（分词器版本变化后使用）。")] = False,
):
    """构建/回填派生词法索引（SQL 统计与评分用，可随时重建）。

    新发布索引自动构建；旧索引（水平扩展阶段 2 之前）与本命令用于回填。分词器版本不一致时
    检索自动回退 Python 参考路径，重建后恢复 SQL 路径。需要 index-write 锁。

    示例：

    \b
      mindex indexes lexical
      mindex indexes lexical --index-id INDEX_ID --replace
    """
    from mindex.shared.lexical_store import build_lexical_index
    from mindex.shared.processing_lock import index_write_lock
    from mindex.shared.storage import Store

    store = Store.open(ctx.obj)
    if ctx.obj.queue.backend == "postgres":
        from mindex.admin.source_tasks import IndexTasks
        from mindex.settings.runtime_settings import RuntimeConfig

        results = IndexTasks(RuntimeConfig(ctx.obj, store), store).run_here(
            {"action": "lexical", "index_id": index_id, "replace": replace})["indexes"]
        print_json({"rebuilt": [r for r in results if not r.get("skipped")],
                    "skipped": [r for r in results if r.get("skipped")]})
        return
    with index_write_lock(store.root, config=ctx.obj):
        if index_id:
            versions = store.rows("SELECT id FROM index_versions WHERE id=? AND status='ready'", (index_id,))
            if not versions:
                raise typer.BadParameter(f"索引不存在或未就绪：{index_id}")
        else:
            versions = store.rows("SELECT id FROM index_versions WHERE status='ready' ORDER BY created_at")
        results = [build_lexical_index(store, row["id"], replace=replace) for row in versions]
    print_json({"rebuilt": [r for r in results if not r.get("skipped")],
                "skipped": [r for r in results if r.get("skipped")]})


@indexes.command("activate")
def activate_index(
    ctx: typer.Context, index_id: Annotated[str, typer.Argument(help="内容索引版本 ID；省略使用活动索引。")]
):
    """检查并激活指定内容索引，可用于发布或回退。

    通过一致性检查后切换活动版本；持久队列由 worker 执行并等待终态。schema 2 空间还会校验当前嵌入身份与能力门槛。回退前先恢复该版本对应的模型服务及配置，不会转换已有向量。

    示例：

    \b
      mindex indexes activate INDEX_ID
    """
    from mindex.processing.indexing import check_index
    from mindex.processing.pipeline import Pipeline
    from mindex.shared.storage import Store

    store = Store.open(ctx.obj)
    if ctx.obj.queue.backend == "postgres":
        from mindex.admin.source_tasks import IndexTasks
        from mindex.settings.runtime_settings import RuntimeConfig

        IndexTasks(RuntimeConfig(ctx.obj, store), store).run_here({"action": "activate", "index_id": index_id})
        typer.echo(f"已发布索引 {index_id}；检索模型须使用该版本对应的配置")
        return
    with Pipeline(ctx.obj).lock():
        issues = check_index(store, index_id, config=ctx.obj)
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
    vacuum: Annotated[
        bool, typer.Option(
            "--vacuum",
            help="配合 --apply 在删除完成后对 metadata 与 chroma 库文件执行 VACUUM 收缩；"
                 "库被其他连接占用（如 serve）时单库跳过并提示，不失败退出。"
                 "单独使用只报告库大小与建议。",
        )
    ] = False,
    days: Annotated[
        int | None, typer.Option(
            "--days", min=0,
            help="把本次运行的全部时间阈值（clean.*_max_age_days、超期 building 与助手附件 "
                 "TTL）临时替换为 N 天：更小更激进，--days 0 即清空全部按龄过期项；不改配置文件。"
                 "硬保护不受影响：现行检查点、活动与保留索引版本、引用中的 attempt、上传宽限与 "
                 "uploads 原件。注意旧转写/分析检查点删除后重生成需 ASR 重听/LLM 重算。",
        )
    ] = None,
):
    """扫描磁盘可回收项并输出 JSON 报告；默认仅预览。

    加 --apply 才实际删除。保留数量与过期天数在 YAML 的 clean 节配置，
    CLI 不提供按类别删除或修改过期阈值的选项。

    A 级：临时残留、孤儿媒体/参考图/音色、人物冗余展示帧、源已删的人物特征
    缓存行、chroma 孤儿 HNSW segment 目录；部分上传与未注册原片残留受
    clean.temp_grace_hours 宽限期保护。

    B 级：按策略清理旧索引（含超期 building 版本）、旧指纹检查点、媒体缓存、
    报告、运行记录，以及 Studio 预览、旧 attempt 和导出 ZIP。
    保留活动索引、clean.index_building_max_age_days 天内的 building 和最近
    clean.index_keep_versions 个非活动 ready 版本。
    删除旧索引后失去该版本的回退与向量复用能力，超期 building 另失去同
    spec 跨运行续跑。

    C 级：模型缓存、e2e/验证产物与数据库备份只在 notes 报告，不自动删除；
    库文件空间回收用 --apply --vacuum 显式执行。

    --days N 把本次运行的全部时间阈值（clean.*_max_age_days、超期 building 与
    助手附件 TTL）临时替换为 N 天，不改配置文件；--days 0 即清空全部按龄过期项。
    硬保护不受影响：现行检查点、活动与保留索引版本、引用中的 attempt、上传宽限
    与 uploads 原件。

    输出 JSON：categories 为逐类别结果，total 为候选数量/字节数，notes 为保留项
    建议，--vacuum 时另有 vacuum 字段。执行过程先在 stdout 输出带时间戳的中文
    进度日志（逐规则扫描、取锁与执行、VACUUM 心跳），JSON 报告在最后整体输出。
    请检查类别的 error/skipped，单类别错误不一定
    使整体退出码为 1。管线或索引锁冲突退出 1；Studio 锁冲突只跳过该部分。
    大规模清理前建议停服。

    示例：

    \b
      mindex clean
      mindex clean --apply
      mindex clean --apply --vacuum
      mindex clean --apply --days 0
      mindex --config /路径/config.yaml --data-dir /路径/data clean
    """
    from mindex.maintenance.clean import CleanBusy, clean_library

    try:
        print_json(clean_library(ctx.obj, apply=apply, vacuum=vacuum, days=days,
                                 progress=_clean_progress))
    except CleanBusy as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None


def _clean_progress(message: str) -> None:
    """清理命令的进度日志：带时间戳写 stdout 并立即刷新，长任务期间可确认仍在工作。"""
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


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

    页面为 /、/admin、/persons、/studio（含 /studio/topics、/studio/templates）、/settings，接口文档 /docs。不启动模型服务；topics.scheduler.enabled 为真时随服务运行每日选题挖掘。--index-id 将检索和相关媒体固定到指定 ready 索引，不更改活动指针。监听非回环地址时必须设置 MINDEX_WEB_TOKEN，或显式传 --allow-insecure。无需前端构建；用 Ctrl+C 停止服务。

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
    from mindex.settings import local_services

    # 每日选题挖掘调度随服务启停（桌面版 sidecar 同样经这里启动）。
    app = create_app(ctx.obj, index_id=index_id, scheduler=ctx.obj.topics.scheduler.enabled)
    warm_up_in_background(ctx.obj, index_id)  # 后台线程，不阻塞监听；失败只记日志
    if ctx.obj.database.backend == "sqlite":
        local_services.autostart_in_background(ctx.obj)
    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        if ctx.obj.database.backend == "sqlite":
            local_services.stop_on_exit(ctx.obj)


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
