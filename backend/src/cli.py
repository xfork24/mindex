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

from mindex.maintenance.backup_cli import app as backup_app
from mindex.shared.config import fingerprint, load_config


class WrappingOptionHighlighter(rich_utils.OptionHighlighter):
    """Keep Typer's colors while folding long Chinese text inside help tables."""

    def __call__(self, text):
        result = super().__call__(text)
        result.overflow = "fold"
        return result


# Rich tables otherwise inherit ellipsis overflow for Chinese parameter descriptions.
rich_utils.highlighter = WrappingOptionHighlighter()


class HelpAwareGroup(TyperGroup):
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
    help="管理内容索引版本：list 查看版本，activate 检查后发布或回退。\n\n"
    "新建索引用 mindex index，一致性检查用 mindex check。回退前恢复对应模型与配置。\n\n"
    "示例：mindex indexes list；mindex indexes activate INDEX_ID。",
    no_args_is_help=True,
)
app.add_typer(indexes, name="indexes")
e2e = typer.Typer(
    help="单 GPU 分阶段真实验证；不会启动或切换远程模型。\n\n"
    "流程：init 初始化 → probe 探测 → pin 固定部署 → phase 分阶段验证；"
    "review 人工复核查询后 freeze 冻结标注，再 evaluate。status 查看进度。\n\n"
    "本组使用 YAML 原值，不合并设置页覆盖。示例：mindex e2e phase --help。",
    no_args_is_help=True,
)
app.add_typer(e2e, name="e2e")


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


def print_json(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


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
):
    """启动同一进程提供的网页、静态资源和 HTTP API。

    页面为 /、/admin、/persons、/studio、/settings，接口文档 /docs。不启动模型服务。--index-id 将检索和相关媒体固定到指定 ready 索引，不更改活动指针。无需前端构建；用 Ctrl+C 停止服务。

    示例：

    \b
      mindex serve --host 127.0.0.1 --port 8000
    """
    import uvicorn

    from mindex.api import create_app

    uvicorn.run(create_app(ctx.obj, index_id=index_id), host=host, port=port)


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
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print_json(report)


class ModelRole(str, Enum):
    embedding = "embedding"
    asr = "asr"
    analyzer = "analyzer"


@e2e.command("init")
def init_experiment(
    ctx: typer.Context,
    source: Annotated[str, typer.Option(help="已完成分镜的素材 ID。")],
    run: Annotated[Path, typer.Option(help="实验目录，保存冻结输入、阶段状态和报告。")],
):
    """从已完成分镜的素材初始化真实验证实验。

    冻结分镜清单，生成待人工填写的标注草稿及查询视频，不调用模型。后续 probe/pin 固定部署，phase 分阶段执行，freeze 记录人工确认；--run 指定实验产物目录。

    示例：

    \b
      mindex e2e init --source SOURCE_ID --run /路径/实验
    """
    from mindex.diagnostics.e2e import initialize
    from mindex.processing.pipeline import Pipeline

    with Pipeline(ctx.obj).lock():
        print_json(initialize(ctx.obj, source, run))


@e2e.command("probe")
def probe_deployment(
    ctx: typer.Context,
    output: Annotated[Path, typer.Option(help="兼容性报告 JSON 文件，失败时也写入。")],
    role: Annotated[
        ModelRole, typer.Option(help="探测或固定的模型角色，读取 YAML 中对应配置。")
    ] = ModelRole.embedding,
):
    """探测已启动的模型服务并保存兼容性报告，不执行推理。

    按 --role 读取 YAML 的服务配置，不启动或切换服务。报告含 ready、端点及兼容信息；探测失败也保存 JSON，并以退出码 1 结束。此结果不等于推理质量或能力验收。

    示例：

    \b
      mindex e2e probe --role embedding --output /路径/probe.json
    """
    from mindex.diagnostics.e2e import save
    from mindex.models.llama_cpp import ModelError, inspect_service

    service = getattr(ctx.obj, role.value)
    try:
        report = {"ready": True, "role": role.value, "base_url": service.base_url, **inspect_service(service)}
    except (ModelError, ValueError, KeyError, TypeError) as exc:
        report = {
            "ready": False,
            "role": role.value,
            "base_url": service.base_url,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    save(output, report)
    print_json(report)
    if not report["ready"]:
        raise typer.Exit(1)


@e2e.command("pin")
def pin_deployment(
    ctx: typer.Context,
    quantization: Annotated[str, typer.Option(help="实际量化标识，例如 Q8_0；写入部署身份。")],
    output: Annotated[Path, typer.Option(help="新部署清单 JSON 路径；已存在时拒绝覆盖。")],
    role: Annotated[
        ModelRole, typer.Option(help="探测或固定的模型角色，读取 YAML 中对应配置。")
    ] = ModelRole.embedding,
    weights_file: Annotated[
        Path | None,
        typer.Option(help="本地 GGUF 文件；与 mmproj-file、server-cwd 同时提供，和远程摘要方式互斥。"),
    ] = None,
    mmproj_file: Annotated[
        Path | None, typer.Option(help="本地视觉投影文件；文件方式必填，流式计算 SHA256。")
    ] = None,
    server_cwd: Annotated[
        Path | None, typer.Option(help="服务器实际工作目录；本地文件方式必填，用于核对模型路径。")
    ] = None,
    weights_sha256: Annotated[
        str | None, typer.Option(help="远程部署模型文件的 SHA256 操作员声明，需同时给出 mmproj-sha256。")
    ] = None,
    mmproj_sha256: Annotated[
        str | None, typer.Option(help="远程视觉投影文件 SHA256；与本地文件方式互斥。")
    ] = None,
    launch_parameters: Annotated[
        Path | None, typer.Option(help="部署者确认的启动参数 JSON 文件；参与部署摘要。")
    ] = None,
):
    """为当前模型部署生成不可覆盖的 schema 2 身份清单。

    先启动对应服务。两种方式互斥：本地同时提供 weights-file、mmproj-file、server-cwd 并计算 SHA256；远程同时声明 weights-sha256、mmproj-sha256。可保存部署者确认的启动参数，远程摘要只代表操作员声明。已有输出文件会拒绝覆盖，换部署使用新文件；后续仍需 smoke 验证。

    示例：

    \b
      mindex e2e pin --quantization Q8_0 --weights-file /模型/model.gguf --mmproj-file /模型/mmproj.gguf --server-cwd /服务目录 --output /路径/deployment.json
    """
    from mindex.diagnostics.e2e import save
    from mindex.models.adapters import ModelError
    from mindex.models.deployment import create_manifest
    from mindex.models.llama_cpp import inspect_service

    if output.exists():
        raise typer.BadParameter("清单已存在；更换部署请使用新文件，保留旧清单")
    try:
        parameters = json.loads(launch_parameters.read_text()) if launch_parameters else {}
        if not isinstance(parameters, dict):
            raise TypeError("启动参数 JSON 必须为对象")
        # 参数错误应在联网/读取大文件前报告。
        local = weights_file is not None or mmproj_file is not None
        remote = weights_sha256 is not None or mmproj_sha256 is not None
        if local == remote:
            raise ValueError("文件方式与远程 SHA 声明互斥，必须选择一种")
        if local and (weights_file is None or mmproj_file is None or server_cwd is None):
            raise ValueError("本地清单需要 --weights-file、--mmproj-file 和 --server-cwd")
        if remote and (weights_sha256 is None or mmproj_sha256 is None):
            raise ValueError("远程声明需要 weights 和 mmproj 两项 SHA256")
        report = inspect_service(getattr(ctx.obj, role.value))
        pinned = create_manifest(
            ctx.obj,
            role.value,
            report,
            quantization,
            weights_file=weights_file,
            mmproj_file=mmproj_file,
            server_cwd=server_cwd,
            weights_sha256=weights_sha256,
            mmproj_sha256=mmproj_sha256,
            launch_parameters=parameters,
        )
        save(output, pinned)
    except (ModelError, ValueError, TypeError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_json(
        {
            "manifest": str(output.resolve()),
            "content_id": pinned["file_identity"]["content_id"],
            "space_id": fingerprint(pinned["space_contract"]) if pinned["space_contract"] else None,
            "deployment_id": pinned["deployment_id"],
            "trust": pinned["trust"]["source"],
        }
    )


@e2e.command("phase")
def experiment_phase(
    ctx: typer.Context,
    phase: Annotated[
        str,
        typer.Argument(
            help="smoke/visual/preview/asr/align/analyze-smoke/analyze/enriched/evaluate/video-evaluate/check。"
        ),
    ],
    run: Annotated[Path, typer.Option(help="实验目录，保存冻结输入、阶段状态和报告。")],
    python: Annotated[
        Path | None, typer.Option(help="仅 align 阶段必填，独立 CPU 对齐环境解释器路径。")
    ] = None,
    queries: Annotated[
        Path | None,
        typer.Option(help="preview 使用草稿；evaluate/video-evaluate 使用已 freeze 的查询 JSONL。"),
    ] = None,
    split: Annotated[
        str, typer.Option(help="查询文件的 split 分组名，例如 dev、test、video；仅评测匹配条目。")
    ] = "test",
    index_id: Annotated[str | None, typer.Option(help="覆盖实验默认索引 ID，用于该阶段查询或检查。")] = None,
    activate: Annotated[bool, typer.Option(help="仅在构建阶段请求发布；缺省构建不改变活动索引。")] = False,
):
    """执行一个真实模型验证阶段并保存实验状态与报告。

    PHASE 可选 smoke、visual、preview、asr、align、analyze-smoke、analyze、enriched、evaluate、video-evaluate、check。align 需 --python；preview 需 --queries 草稿；evaluate/video-evaluate 需 freeze 后的标注。构建阶段默认不发布，查询默认使用实验记录的索引。video-evaluate 的默认 test 分组会映射为 video。不会自动启动或切换服务，按阶段准备模型；失败输出结构化错误并退出 1。

    示例：

    \b
      mindex e2e phase visual --run /路径/实验
    """
    from filelock import Timeout

    from mindex.diagnostics.e2e import run_phase
    from mindex.models.adapters import ModelError
    from mindex.shared.media import MediaError

    try:
        result = run_phase(
            ctx.obj,
            run,
            phase,
            python=python,
            queries=queries,
            split=split,
            index_id=index_id,
            activate=activate,
        )
    except (ModelError, MediaError, ValueError, OSError, Timeout) as exc:
        print_json({"phase": phase, "ok": False, "error": str(exc), "error_type": type(exc).__name__})
        raise typer.Exit(1) from exc
    print_json(result)


@e2e.command("freeze")
def freeze_labels(
    queries: Annotated[Path, typer.Argument(help="查询标注 JSONL 文件路径。")],
    run: Annotated[Path, typer.Option(help="实验目录，保存冻结输入、阶段状态和报告。")],
    confirmed_by: Annotated[str, typer.Option(help="实际完成人工复核的姓名或标识，将写入冻结清单。")],
):
    """记录人工确认者并冻结查询标注，生成 SHA256 清单。

    先逐条复核查询与目标；此命令不会替用户判断标注正确性。冻结后修改查询文件需重新确认冻结，否则评测拒绝不匹配的摘要。

    示例：

    \b
      mindex e2e freeze /路径/queries.jsonl --run /路径/实验 --confirmed-by '姓名'
    """
    from mindex.diagnostics.e2e import freeze

    print_json({"frozen": freeze(run, queries, confirmed_by)})


@e2e.command("review")
def review_labels(
    ctx: typer.Context,
    queries: Annotated[Path, typer.Argument(help="查询标注 JSONL 文件路径。")],
    run: Annotated[Path, typer.Option(help="实验目录，保存冻结输入、阶段状态和报告。")],
):
    """生成可播放候选并导出标注的本地复核页面。

    读取实验与查询文件，输出 review_page 路径；在浏览器中人工查看并选择目标。不会自动确认标签，最终评测前仍需 freeze。

    示例：

    \b
      mindex e2e review /路径/queries.jsonl --run /路径/实验
    """
    from mindex.diagnostics.label_review import render_review

    print_json({"review_page": str(render_review(ctx.obj, run.resolve(), queries))})


@e2e.command("status")
def experiment_status(
    ctx: typer.Context, run: Annotated[Path, typer.Option(help="实验目录，保存冻结输入、阶段状态和报告。")]
):
    """查看实验阶段状态、活动索引和当前配置的分析完成数量。

    读取 --run 的状态与素材库，不请求模型或推进处理阶段，输出 JSON。

    示例：

    \b
      mindex e2e status --run /路径/实验
    """
    from mindex.diagnostics.e2e import progress

    print_json(progress(ctx.obj, run))


persons_cli = typer.Typer(
    help="管理演员、作品、角色、参考图和人物检索发布。\n\n"
    "流程：save 登记实体 → reference 添加参考图 → process 扫描 → status 查看任务 → "
    "review 审核 → publish 发布；activate 回退已有检索组合。migrate-frames 迁移旧展示帧。\n\n"
    "reference/process 需要人物服务，任务后台执行。示例：mindex persons process --help。",
    no_args_is_help=True,
)
app.add_typer(persons_cli, name="persons")


@persons_cli.command("list")
def persons_list(ctx: typer.Context):
    """列出人物库的实体与参考图登记信息。

    输出 entities、references 的 JSON；实体包括演员、作品和角色。无需人物服务，不执行识别或发布。

    示例：

    \b
      mindex persons list
    """
    from mindex.persons.persons import Persons
    from mindex.shared.storage import Store

    catalogue = Persons(Store(ctx.obj.data_dir))
    print_json({"entities": catalogue.entities(), "references": catalogue.references()})


@persons_cli.command("save")
def persons_save(
    ctx: typer.Context,
    file: Annotated[
        Path, typer.Argument(help="实体 JSON 文件；kind=actor/work/role，name 必填，角色需 work_ids。")
    ],
    identifier: Annotated[str | None, typer.Option(help="要修改的实体 ID；省略则新建。")] = None,
):
    """从 JSON 新建或修改演员、作品、角色实体。

    必填 kind（actor/work/role）与 name；可填 aliases、work_ids、actor_ids。角色至少关联一个 work_id。--identifier 指定修改对象，省略则新建；不会自动添加参考图或发布检索组合。

    示例：

    \b
      mindex persons save /路径/actor.json
    """
    from mindex.persons.persons import Entity, Persons
    from mindex.shared.storage import Store

    print_json(
        Persons(Store(ctx.obj.data_dir)).save(Entity.model_validate_json(file.read_text()), identifier)
    )


@persons_cli.command("reference")
def persons_reference(
    ctx: typer.Context,
    actor: Annotated[str, typer.Argument(help="已登记演员实体 ID。")],
    images: Annotated[list[Path], typer.Argument(help="一张或多张本地参考图片；需要人物服务质检。")],
):
    """为指定演员添加一张或多张参考图并进行质量检查。

    ACTOR 使用人物库演员 ID，IMAGES 为本地图片路径。需要可用人物服务，质检失败或服务不可用则拒绝入库，输出参考图 JSON；原尺寸图像保存在人物库。

    示例：

    \b
      mindex persons reference ACTOR_ID /路径/正脸.png /路径/侧脸.jpg
    """
    from mindex.persons.person_processing import PersonClient, ensure_reference_quality
    from mindex.persons.persons import Persons
    from mindex.shared.storage import Store

    config = ctx.obj
    store = Store(config.data_dir)
    catalogue = Persons(store)
    client, space = PersonClient(config), None

    def check(item):
        nonlocal space
        space = ensure_reference_quality(store, config, client, item, space=space)

    print_json([catalogue.add_reference(actor, p.read_bytes(), checker=check) for p in images])


@persons_cli.command("process")
def persons_process(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="已登记素材 ID。")],
    start: Annotated[float, typer.Option(help="原片扫描开始时间，单位秒。")] = 0,
    end: Annotated[float | None, typer.Option(help="原片扫描结束时间，单位秒；省略处理至结尾。")] = None,
    publish: Annotated[bool, typer.Option(help="请求任务完成后发布人物检索组合；默认只处理。")] = False,
    retry: Annotated[
        str | None, typer.Option(help="同素材 failed/interrupted 任务 ID，复用该任务重试。")
    ] = None,
):
    """登记人物扫描任务并启动独立后台进程，立即返回任务信息。

    SOURCE 是库内素材 ID，需人物服务与参考图；可限制原片起止秒数。输出 task_id、queued 状态和日志路径，使用 persons status 轮询。--retry 仅接受同素材 failed/interrupted 任务 ID；默认不发布，--publish 请求完成后发布。

    示例：

    \b
      mindex persons process SOURCE_ID --start 0 --end 60
    """
    import subprocess
    import sys

    from mindex.persons.person_processing import PersonOptions
    from mindex.persons.persons import uid
    from mindex.shared.processing_lock import ProcessingLock
    from mindex.shared.storage import Store, dumps

    store = Store(ctx.obj.data_dir)
    options = PersonOptions(start=start, end=end, publish=publish)
    if not store.one("SELECT id FROM sources WHERE id=?", (source,)):
        raise typer.BadParameter("找不到素材")
    with ProcessingLock(ctx.obj, source_id=source, work="persons"):
        identifier = retry or uid()
        if retry:
            row = store.one("SELECT * FROM person_tasks WHERE id=?", (retry,))
            if not row or row["status"] not in {"failed", "interrupted"} or row["source_id"] != source:
                raise typer.BadParameter("重试任务必须失败/中断且归属该素材")
            store.execute(
                "UPDATE person_tasks SET status='queued',error=NULL,result=NULL WHERE id=?", (retry,)
            )
        else:
            store.execute(
                "INSERT INTO person_tasks(id,source_id,status,config,options) VALUES(?,?,?,?,?)",
                (identifier, source, "queued", ctx.obj.model_dump_json(), dumps(options.model_dump())),
            )
    log_path = store.root / "persons" / "task-logs" / (identifier + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("ab") as handle:
            subprocess.Popen(
                [sys.executable, "-m", "mindex.persons.person_worker", str(store.root), identifier],
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                start_new_session=True,
            )
    except OSError as exc:
        store.execute("UPDATE person_tasks SET status='failed',error=? WHERE id=?", (str(exc), identifier))
        raise
    print_json({"task_id": identifier, "status": "queued", "log": str(log_path)})


@persons_cli.command("status")
def persons_status(
    ctx: typer.Context,
    task: Annotated[str, typer.Argument(help="人物后台任务 ID，来自 persons process 输出。")],
):
    """查看一个后台人物任务的状态、结果与错误。

    TASK 是 persons process 返回的任务 ID，不是素材 ID。读取已持久化记录并输出 JSON，不触发推理或重试。

    示例：

    \b
      mindex persons status TASK_ID
    """
    from mindex.persons.person_processing import PersonTasks
    from mindex.shared.storage import Store

    manager = PersonTasks.__new__(PersonTasks)
    manager.store = Store(ctx.obj.data_dir)
    print_json(manager.get(task))


@persons_cli.command("publish")
def persons_publish(
    ctx: typer.Context,
    content: Annotated[str | None, typer.Option(help="绑定的 ready 内容索引 ID；省略用活动索引。")] = None,
):
    """冻结当前已接受的人物证据并发布内容索引与人物快照的检索组合。

    默认绑定当前活动内容索引，或用 --content 指定 ready 索引。无需模型推理；未接受证据不会自动被批准。人物发布与内容建库独立，输出版本及组合信息。

    示例：

    \b
      mindex persons publish --content INDEX_ID
    """
    from mindex.persons.persons import Persons
    from mindex.shared.processing_lock import ProcessingLock, index_write_lock
    from mindex.shared.storage import Store

    store = Store(ctx.obj.data_dir)
    with ProcessingLock(ctx.obj), index_write_lock(store.root):
        print_json(Persons(store).publish(content))


@persons_cli.command("review")
def persons_review(
    ctx: typer.Context,
    file: Annotated[
        Path, typer.Argument(help="审核 JSON：source_id、shot_id、selections（actor_id 与可选 role_id）。")
    ],
):
    """从 JSON 人工确认一个镜头的出镜身份。

    输入包含 source_id、shot_id、selections；每个 selection 含 actor_id 与可选 role_id。写入审核结果，不调用识别服务；要用于已发布检索需再执行 persons publish。

    示例：

    \b
      mindex persons review /路径/review.json
    """
    from mindex.persons.persons import Persons, ShotReview
    from mindex.shared.storage import Store

    print_json(Persons(Store(ctx.obj.data_dir)).shot_review(ShotReview.model_validate_json(file.read_text())))


@persons_cli.command("activate")
def persons_activate(
    ctx: typer.Context,
    release: Annotated[str, typer.Argument(help="已有检索组合 ID，包含内容索引和人物版本的组合。")],
):
    """激活已有内容索引与人物快照的检索组合。

    RELEASE 是检索组合 ID，不是单独的人物版本或内容索引 ID。组合必须存在且内容索引 ready、成员完整；会同时切换活动内容索引和检索组合指针。查询前应恢复匹配的嵌入配置。

    示例：

    \b
      mindex persons activate RELEASE_ID
    """
    from mindex.shared.storage import Store

    store = Store(ctx.obj.data_dir)
    store.activate_release(release)
    print_json({"release_id": release})


clips = typer.Typer(
    help="维护旧切片与 shot 媒体引用，使用 YAML 原配置。\n\n"
    "migrate 盘点/迁移，verify-archives 校验归档原片，repair-boundaries 诊断/修复切点；"
    "验收迁移后 prune 删除旧实体，materialize --all 从 shot 恢复实体。\n\n"
    "migrate、repair-boundaries、prune 默认预览；materialize --all 直接写文件。"
    "通用磁盘回收用 mindex clean。示例：mindex clips migrate --help。",
    no_args_is_help=True,
)
app.add_typer(clips, name="clips")


@clips.command("migrate")
def clips_migrate(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Option(help="素材 ID；省略处理全部素材，不是文件路径。")] = None,
    apply: Annotated[bool, typer.Option(help="实际写入或删除；缺省仅输出只读预览。")] = False,
    verify: Annotated[
        bool, typer.Option(help="与 --apply 配合验证迁移媒体；仅预览时不执行媒体验证。")
    ] = False,
):
    """盘点旧切片并迁移为 shot 媒体引用，默认只读。

    --apply 备份并登记引用；--verify 与 --apply 配合验证媒体，生成后续 prune 使用的迁移报告。此命令不物理删除旧实体。省略 --source 处理全部素材，使用 YAML 原配置而非设置页覆盖层。

    示例：

    \b
      mindex clips migrate --source SOURCE_ID --apply --verify
    """
    from mindex.maintenance.clips import migrate

    print_json(migrate(ctx.obj, source, apply=apply, verify=verify))


@clips.command("materialize")
def clips_materialize(
    ctx: typer.Context,
    all: Annotated[
        bool, typer.Option("--all", help="必须显式指定，恢复全部可恢复的旧媒体路径实体。")
    ] = False,
):
    """从现有 shot 恢复旧路径下的媒体实体文件。

    必须显式加 --all；此操作直接生成文件，没有只读预览开关。用于恢复需要实体的旧媒体路径，不重新调用模型。

    示例：

    \b
      mindex clips materialize --all
    """
    if not all:
        raise typer.BadParameter("请指定 --all")
    from mindex.maintenance.clips import materialize_all

    print_json(materialize_all(ctx.obj))


@clips.command("verify-archives")
def clips_verify_archives(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Option(help="素材 ID；省略处理全部素材，不是文件路径。")] = None,
):
    """计算归档原片 SHA256 并校验完整性。

    省略 --source 校验全部归档；大文件可能耗时。输出 JSON，普通 check 不执行这个完整原片哈希检查。

    示例：

    \b
      mindex clips verify-archives --source SOURCE_ID
    """
    from mindex.maintenance.clips import verify_archives

    print_json(verify_archives(ctx.obj, source))


@clips.command("repair-boundaries")
def clips_repair_boundaries(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Option(help="素材 ID；省略处理全部素材，不是文件路径。")] = None,
    audit_file: Annotated[
        Path | None, typer.Option(help="读取已有审计 JSON；省略则根据 --source 现场诊断。")
    ] = None,
    apply: Annotated[bool, typer.Option(help="实际写入或删除；缺省仅输出只读预览。")] = False,
):
    """诊断原片中的镜头切点并按审计报告修复，默认只读预览。

    可按 --source 生成诊断，也可用 --audit-file 读取已有 JSON；--apply 才执行修复，保留旧媒体。应先检查报告，避免直接对生产素材试运行修复。

    示例：

    \b
      mindex clips repair-boundaries --source SOURCE_ID
    """
    from mindex.processing.shot_repair import apply as repair
    from mindex.processing.shot_repair import audit
    from mindex.shared.storage import Store

    report = json.loads(audit_file.read_text()) if audit_file else audit(Store(ctx.obj.data_dir), source)
    print_json(repair(ctx.obj, report) if apply else report)


@clips.command("prune")
def clips_prune(
    ctx: typer.Context,
    migration_report: Annotated[Path, typer.Argument(help="已通过验证的迁移报告 JSON 路径。")],
    apply: Annotated[bool, typer.Option(help="实际写入或删除；缺省仅输出只读预览。")] = False,
):
    """根据已验证的迁移报告预览或删除旧切片实体。

    MIGRATION_REPORT 是 clips migrate --apply --verify 生成的报告文件。先验收引用与播放，再加 --apply 物理删除；保留原片与 shot，不作为通用磁盘清理入口。

    示例：

    \b
      mindex clips prune /路径/migration-report.json
    """
    from mindex.maintenance.clips import prune

    print_json(prune(ctx.obj, migration_report, apply=apply))


@persons_cli.command("migrate-frames")
def persons_migrate_frames(
    ctx: typer.Context,
    apply: Annotated[bool, typer.Option(help="实际写入或删除；缺省仅输出只读预览。")] = False,
    source: Annotated[str | None, typer.Option(help="素材 ID；省略处理全部素材，不是文件路径。")] = None,
):
    """迁移、压缩人物展示帧并清理冗余帧，默认只预览。

    原尺寸参考图和导入图保留；无需重新索引。--source 限定素材，--apply 才实际写入及清理。输出逐素材结果，任一素材出错则退出 1。

    示例：

    \b
      mindex persons migrate-frames --source SOURCE_ID --apply
    """
    from mindex.persons.persons import migrate_frames

    try:
        report = migrate_frames(ctx.obj, source_id=source, apply=apply)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    print_json(report)
    if any(row["error"] for row in report["sources"]):
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
