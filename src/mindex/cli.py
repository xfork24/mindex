from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

from .config import fingerprint, load_config

app = typer.Typer(help="中文视频素材离线处理与检索", no_args_is_help=True)
indexes = typer.Typer(help="索引版本与回退", no_args_is_help=True)
app.add_typer(indexes, name="indexes")
e2e = typer.Typer(help="单 GPU 分阶段真实验证；不会启动或切换远程模型", no_args_is_help=True)
app.add_typer(e2e, name="e2e")


class IndexMode(str, Enum):
    auto = "auto"
    visual = "visual"
    enriched = "enriched"


@app.callback()
def configure(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    verbose: bool = False,
    no_runtime_overrides: Annotated[bool, typer.Option("--no-runtime-overrides", help="固定使用 YAML，禁用 CLI 与 Web 动态覆盖")] = False,
):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(message)s")
    ctx.obj = load_config(config)
    if no_runtime_overrides:
        ctx.obj.runtime_overrides = False
    # 设置页保存的覆盖层对 CLI 同样生效（Web 与 CLI 行为一致）；e2e 验证框架保持 yaml 原值。
    if ctx.invoked_subcommand not in {"e2e", "clips"}:
        from .runtime_settings import apply_runtime_overrides

        ctx.obj = apply_runtime_overrides(ctx.obj)


def print_json(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@app.command()
def register(ctx: typer.Context, videos: list[Path]):
    """只验证、去重并登记原片，不启动分镜或模型处理。"""
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    for path in videos:
        print_json(pipeline.register(path))


@app.command()
def prepare(ctx: typer.Context, videos: list[Path]):
    """导入原视频、检测镜头并实际切片。"""
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock():
        for path in videos:
            typer.echo(pipeline.prepare(path))


@app.command()
def detect(ctx: typer.Context, video: Path):
    """只扫描并保存分镜计划，不切片、不调用嵌入模型。"""
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock():
        typer.echo(pipeline.detect(video))


@app.command()
def split(ctx: typer.Context, source: str):
    """按已保存计划切片；重跑可恢复，不重新扫描整部电影。"""
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock(source, "shots"):
        pipeline.split(source)
    typer.echo(source)


@app.command()
def inspect(ctx: typer.Context, source: str):
    """查看电影分镜数量、时长分布、切片进度及缺失文件。"""
    from .film import source_report

    print_json(source_report(ctx.obj, source))


@app.command("export")
def export_source(ctx: typer.Context, source: str, output: Path | None = None):
    """导出 JSON、CSV 和本地视频分镜检查页面。"""
    from .film import export_source as run_export
    from .pipeline import Pipeline

    with Pipeline(ctx.obj).lock():
        print_json(run_export(ctx.obj, source, output))


@app.command()
def transcribe(ctx: typer.Context, source: str | None = None):
    """批量转写；MiniMax 直接保存词级时间戳，旧纯文本可手动 align。"""
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock(source, "dialogue"):
        pipeline.transcribe(source)


@app.command("enrich-dialogue")
def enrich_dialogue(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Argument()] = None,
    subtitle_stream: Annotated[int | None, typer.Option(min=0)] = None,
    method: Annotated[str, typer.Option(help="对白方式：auto、subtitle、asr")] = "auto",
    no_publish: Annotated[bool, typer.Option("--no-publish")] = False,
    workers: Annotated[int, typer.Option(min=1, max=8)] = 2,
):
    """字幕优先补充对白，断点续跑；默认检查并自动发布，无 LLM 分析。"""
    from .dialogue_tasks import execute_dialogue
    from .pipeline import Pipeline

    with Pipeline(ctx.obj).lock(source, "dialogue"):
        print_json(execute_dialogue(ctx.obj, source, method=method, subtitle_stream=subtitle_stream,
                                   publish=not no_publish, workers=workers,
                                   progress=lambda value: typer.echo(json.dumps(value, ensure_ascii=False))))


@app.command()
def align(ctx: typer.Context, python: Path, source: str | None = None):
    """用独立 CPU ForcedAligner 环境对齐已缓存的原始 ASR 文本。"""
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock(source, "dialogue"):
        pipeline.align(python, source)


@app.command()
def analyze(ctx: typer.Context, source: str | None = None):
    """分析镜头和精细片段，组织连续场景；失败后可重跑。"""
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock(source, "analysis"):
        pipeline.analyze(source)


@app.command(name="index")
def build_index(
    ctx: typer.Context,
    activate: bool = False,
    mode: IndexMode = IndexMode.auto,
    channel: Annotated[list[str] | None, typer.Option(help="可重复指定 visual/dialogue/analysis")] = None,
    source: str | None = None,
    kind: Annotated[
        list[str] | None, typer.Option(help="可重复指定 shot/moment/scene；visual 默认 moment")
    ] = None,
):
    """建立或继续新索引，--activate 在检查通过后发布。"""
    from .indexing import IndexBuilder
    from .pipeline import Pipeline

    with Pipeline(ctx.obj).lock(source, "publish"):
        typer.echo(
            IndexBuilder(ctx.obj).build(
                activate=activate, mode=mode.value, source_id=source, kinds=tuple(kind) if kind else None, channels=channel
            )
        )


@app.command()
def ingest(ctx: typer.Context, videos: list[Path], activate: bool = False):
    """导入、补充对白并建库；不触发 LLM 分析。"""
    from .indexing import IndexBuilder
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock():
        for path in videos:
            pipeline.prepare(path)
        pipeline.enrich_dialogue()
        typer.echo(IndexBuilder(ctx.obj).build(activate=activate))


@app.command()
def search(ctx: typer.Context, text: str, granularity: str = "moment", limit: int = 20,
           channel: Annotated[list[str] | None, typer.Option()] = None):
    from .search import SearchEngine
    from .types import TextSearch

    print_json(SearchEngine(ctx.obj).text(TextSearch(text=text, granularity=granularity, limit=limit, channels=channel)))


@app.command()
def status(ctx: typer.Context, health: bool = False):
    from .search import status as get_status

    print_json(get_status(ctx.obj, health=health))


@app.command()
def check(ctx: typer.Context, index_id: str | None = None):
    from .indexing import check_index
    from .storage import Store

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
    from .storage import Store

    store = Store(ctx.obj.data_dir)
    print_json(
        {
            "active": store.active_index(),
            "versions": store.rows("SELECT * FROM index_versions ORDER BY created_at DESC"),
        }
    )


@indexes.command("activate")
def activate_index(ctx: typer.Context, index_id: str):
    from .indexing import check_index
    from .pipeline import Pipeline
    from .storage import Store

    store = Store(ctx.obj.data_dir)
    with Pipeline(ctx.obj).lock():
        issues = check_index(store, index_id)
        if issues:
            raise typer.BadParameter("索引检查失败：" + "; ".join(issues[:5]))
        version = store.index_version(index_id)
        if version["spec"]["space"].get("schema") == 2:
            from .adapters import make_embedder

            client = make_embedder(ctx.obj)
            if client.identity() != version["spec"]["space"]:
                raise typer.BadParameter("当前模型空间与待发布索引不同")
            client.require_gate()
        store.activate(index_id)
    typer.echo(f"已发布索引 {index_id}；检索模型须使用该版本对应的配置")


@app.command()
def clean(
    ctx: typer.Context,
    apply: Annotated[bool, typer.Option("--apply", help="实际删除；缺省仅预览可回收内容")] = False,
):
    """扫描可安全回收的磁盘数据；默认预览，加 --apply 才真正删除。"""
    from .clean import CleanBusy, clean_library

    try:
        print_json(clean_library(ctx.obj, apply=apply))
    except CleanBusy as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None


@app.command()
def serve(ctx: typer.Context, host: str = "127.0.0.1", port: int = 8000, index_id: str | None = None):
    """启动页面和检索 API。"""
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(ctx.obj, index_id=index_id), host=host, port=port)


@app.command()
def evaluate(
    ctx: typer.Context,
    queries: Path,
    split: str = "test",
    output: Path | None = None,
    index_id: str | None = None,
):
    from .evaluation import evaluate as run_evaluation

    report = run_evaluation(ctx.obj, queries, split, output, index_id=index_id)
    print_json({k: {a: b for a, b in v.items() if a != "details"} for k, v in report["profiles"].items()})


@app.command("model-check")
def check_model(
    ctx: typer.Context,
    video: Path,
    text: str = "一个人在房间里走动",
    repeats: Annotated[int, typer.Option(min=1, max=20)] = 3,
    output: Path | None = None,
):
    """验证真实文字/视频嵌入、空间身份、耗时及 GPU 进程显存。"""
    from .benchmark import model_check

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
    ctx: typer.Context, source: Annotated[str, typer.Option()], run: Annotated[Path, typer.Option()]
):
    """冻结已有分镜、生成标注草稿和 12 个查询视频；不调用模型。"""
    from .e2e import initialize
    from .pipeline import Pipeline

    with Pipeline(ctx.obj).lock():
        print_json(initialize(ctx.obj, source, run))


@e2e.command("probe")
def probe_deployment(
    ctx: typer.Context, output: Annotated[Path, typer.Option()], role: ModelRole = ModelRole.embedding
):
    """只探测已启动服务，保存兼容性报告；不执行推理。"""
    from .e2e import save
    from .llama_cpp import ModelError, inspect_service

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
    quantization: Annotated[str, typer.Option()],
    output: Annotated[Path, typer.Option()],
    role: ModelRole = ModelRole.embedding,
    weights_file: Annotated[Path | None, typer.Option(help="本地 GGUF 文件，流式计算 SHA256")] = None,
    mmproj_file: Annotated[Path | None, typer.Option(help="本地投影文件，流式计算 SHA256")] = None,
    server_cwd: Annotated[Path | None, typer.Option(help="服务器实际工作目录，本地核验时必填")] = None,
    weights_sha256: Annotated[str | None, typer.Option(help="远程部署的操作员声明 SHA256")] = None,
    mmproj_sha256: Annotated[str | None, typer.Option(help="远程部署的操作员声明 SHA256")] = None,
    launch_parameters: Annotated[Path | None, typer.Option(help="部署者确认的启动参数 JSON 文件")] = None,
):
    """创建 schema 2 清单；文件方式与远程 SHA 声明互斥，不覆盖旧清单。"""
    from .adapters import ModelError
    from .deployment import create_manifest
    from .e2e import save
    from .llama_cpp import inspect_service

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
            ctx.obj, role.value, report, quantization, weights_file=weights_file, mmproj_file=mmproj_file,
            server_cwd=server_cwd, weights_sha256=weights_sha256, mmproj_sha256=mmproj_sha256,
            launch_parameters=parameters,
        )
        save(output, pinned)
    except (ModelError, ValueError, TypeError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_json({"manifest": str(output.resolve()), "content_id": pinned["file_identity"]["content_id"],
                "space_id": fingerprint(pinned["space_contract"]) if pinned["space_contract"] else None,
                "deployment_id": pinned["deployment_id"], "trust": pinned["trust"]["source"]})


@e2e.command("phase")
def experiment_phase(
    ctx: typer.Context,
    phase: str,
    run: Annotated[Path, typer.Option()],
    python: Path | None = None,
    queries: Path | None = None,
    split: str = "test",
    index_id: str | None = None,
    activate: bool = False,
):
    """smoke → visual → preview → asr → align → analyze(-smoke) → enriched → evaluate/video-evaluate → check。"""
    from filelock import Timeout

    from .adapters import ModelError
    from .e2e import run_phase
    from .media import MediaError

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
    queries: Path, run: Annotated[Path, typer.Option()], confirmed_by: Annotated[str, typer.Option()]
):
    """人工确认每条标注后冻结并记录 SHA256；不自动批准标注。"""
    from .e2e import freeze

    print_json({"frozen": freeze(run, queries, confirmed_by)})


@e2e.command("review")
def review_labels(ctx: typer.Context, queries: Path, run: Annotated[Path, typer.Option()]):
    """生成可播放候选和导出标注的本地复核页面；不会自动确认。"""
    from .label_review import render_review

    print_json({"review_page": str(render_review(ctx.obj, run.resolve(), queries))})


@e2e.command("status")
def experiment_status(ctx: typer.Context, run: Annotated[Path, typer.Option()]):
    """查看阶段状态和当前分析配置的完成数量，不请求模型。"""
    from .e2e import progress

    print_json(progress(ctx.obj, run))




persons_cli = typer.Typer(help="人物库、人物任务与独立检索发布", no_args_is_help=True)
app.add_typer(persons_cli, name="persons")


@persons_cli.command('list')
def persons_list(ctx: typer.Context):
    from .persons import Persons
    from .storage import Store
    catalogue = Persons(Store(ctx.obj.data_dir))
    print_json({'entities': catalogue.entities(), 'references': catalogue.references()})


@persons_cli.command('save')
def persons_save(ctx: typer.Context, file: Path, identifier: str | None = None):
    """从实体 JSON 新建或修改演员、作品或角色。"""
    from .persons import Entity, Persons
    from .storage import Store
    print_json(Persons(Store(ctx.obj.data_dir)).save(Entity.model_validate_json(file.read_text()), identifier))


@persons_cli.command('reference')
def persons_reference(ctx: typer.Context, actor: str, images: list[Path]):
    """添加演员参考图；入库前与页面同口径质检（人物服务不可用即拒绝）。"""
    from .person_processing import PersonClient, ensure_reference_quality
    from .persons import Persons
    from .storage import Store
    config = ctx.obj
    store = Store(config.data_dir)
    catalogue = Persons(store)
    client, space = PersonClient(config), None

    def check(item):
        nonlocal space
        space = ensure_reference_quality(store, config, client, item, space=space)

    print_json([catalogue.add_reference(actor, p.read_bytes(), checker=check) for p in images])


@persons_cli.command('process')
def persons_process(ctx: typer.Context, source: str, start: float = 0, end: float | None = None,
                    publish: bool = False, retry: str | None = None):
    """持久化任务并启动独立工作进程，立即返回任务 ID。"""
    import subprocess
    import sys

    from .person_processing import PersonOptions
    from .persons import uid
    from .processing_lock import ProcessingLock
    from .storage import Store, dumps
    store = Store(ctx.obj.data_dir)
    options = PersonOptions(start=start, end=end, publish=publish)
    if not store.one('SELECT id FROM sources WHERE id=?', (source,)):
        raise typer.BadParameter('找不到素材')
    with ProcessingLock(ctx.obj, source_id=source, work="persons"):
        identifier = retry or uid()
        if retry:
            row = store.one('SELECT * FROM person_tasks WHERE id=?', (retry,))
            if not row or row['status'] not in {'failed', 'interrupted'} or row['source_id'] != source:
                raise typer.BadParameter('重试任务必须失败/中断且归属该素材')
            store.execute("UPDATE person_tasks SET status='queued',error=NULL,result=NULL WHERE id=?", (retry,))
        else:
            store.execute('INSERT INTO person_tasks(id,source_id,status,config,options) VALUES(?,?,?,?,?)',
                (identifier, source, 'queued', ctx.obj.model_dump_json(), dumps(options.model_dump())))
    log_path = store.root / 'persons' / 'task-logs' / (identifier + '.log')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open('ab') as handle:
            subprocess.Popen([sys.executable, '-m', 'mindex.person_worker', str(store.root), identifier],
                             stdin=subprocess.DEVNULL, stdout=handle, stderr=handle, start_new_session=True)
    except OSError as exc:
        store.execute("UPDATE person_tasks SET status='failed',error=? WHERE id=?", (str(exc), identifier))
        raise
    print_json({'task_id': identifier, 'status': 'queued', 'log': str(log_path)})


@persons_cli.command('status')
def persons_status(ctx: typer.Context, task: str):
    from .person_processing import PersonTasks
    from .storage import Store
    manager = PersonTasks.__new__(PersonTasks)
    manager.store = Store(ctx.obj.data_dir)
    print_json(manager.get(task))


@persons_cli.command('publish')
def persons_publish(ctx: typer.Context, content: str | None = None):
    from .persons import Persons
    from .processing_lock import ProcessingLock, index_write_lock
    from .storage import Store
    store = Store(ctx.obj.data_dir)
    with ProcessingLock(ctx.obj), index_write_lock(store.root):
        print_json(Persons(store).publish(content))


@persons_cli.command('review')
def persons_review(ctx: typer.Context, file: Path):
    """从 JSON 确认分镜出镜人物（source_id、shot_id、selections）。"""
    from .persons import Persons, ShotReview
    from .storage import Store
    print_json(Persons(Store(ctx.obj.data_dir)).shot_review(ShotReview.model_validate_json(file.read_text())))


@persons_cli.command('activate')
def persons_activate(ctx: typer.Context, release: str):
    from .storage import Store
    store = Store(ctx.obj.data_dir)
    store.activate_release(release)
    print_json({'release_id': release})




clips = typer.Typer(help="Shot 媒体引用迁移与实体恢复", no_args_is_help=True)
app.add_typer(clips, name="clips")


@clips.command("migrate")
def clips_migrate(ctx: typer.Context, source: str | None = None, apply: bool = False, verify: bool = False):
    """默认只读盘点；--apply 备份并登记引用；--verify 验证媒体，均不删除实体。"""
    from .clips import migrate

    print_json(migrate(ctx.obj, source, apply=apply, verify=verify))


@clips.command("materialize")
def clips_materialize(ctx: typer.Context, all: Annotated[bool, typer.Option("--all")] = False):
    """仅从 shot 恢复旧路径下的实体。"""
    if not all:
        raise typer.BadParameter("请指定 --all")
    from .clips import materialize_all

    print_json(materialize_all(ctx.obj))


@clips.command("verify-archives")
def clips_verify_archives(ctx: typer.Context, source: str | None = None):
    """显式校验归档原片 SHA256；业务索引检查不执行此操作。"""
    from .clips import verify_archives

    print_json(verify_archives(ctx.obj, source))


@clips.command("repair-boundaries")
def clips_repair_boundaries(ctx: typer.Context, source: str | None = None,
                           audit_file: Path | None = None, apply: bool = False):
    """显式原片诊断/修复分镜切点；旧媒体保留，默认只读预览。"""
    from .shot_repair import apply as repair
    from .shot_repair import audit
    from .storage import Store

    report = json.loads(audit_file.read_text()) if audit_file else audit(Store(ctx.obj.data_dir), source)
    print_json(repair(ctx.obj, report) if apply else report)


@clips.command("prune")
def clips_prune(ctx: typer.Context, migration_report: Path, apply: bool = False):
    """预览已验证旧切片；验收后 --apply 物理删除，保留原片和 shot。"""
    from .clips import prune

    print_json(prune(ctx.obj, migration_report, apply=apply))


if __name__ == "__main__":
    app()
