from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

from .config import load_config

app = typer.Typer(help="中文视频素材离线处理与检索", no_args_is_help=True)
indexes = typer.Typer(help="索引版本与回退", no_args_is_help=True)
app.add_typer(indexes, name="indexes")
e2e = typer.Typer(help="单 GPU 分阶段真实验证；不会启动或切换远程模型", no_args_is_help=True)
app.add_typer(e2e, name="e2e")


class IndexMode(str, Enum):
    visual = "visual"
    enriched = "enriched"


@app.callback()
def configure(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    verbose: bool = False,
):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(message)s")
    ctx.obj = load_config(config)


def print_json(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


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
    with pipeline.lock():
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
    """批量转写；完成后可停止 ASR 服务并切换 Qwen。"""
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock():
        pipeline.transcribe(source)


@app.command()
def align(ctx: typer.Context, python: Path, source: str | None = None):
    """用独立 CPU ForcedAligner 环境对齐已缓存的原始 ASR 文本。"""
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock():
        pipeline.align(python, source)


@app.command()
def analyze(ctx: typer.Context, source: str | None = None):
    """分析镜头和精细片段，组织连续场景；失败后可重跑。"""
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock():
        pipeline.analyze(source)


@app.command(name="index")
def build_index(
    ctx: typer.Context,
    activate: bool = False,
    mode: IndexMode = IndexMode.enriched,
    source: str | None = None,
    kind: Annotated[
        list[str] | None, typer.Option(help="可重复指定 shot/moment/scene；visual 默认 moment")
    ] = None,
):
    """建立或继续新索引，--activate 在检查通过后发布。"""
    from .indexing import IndexBuilder
    from .pipeline import Pipeline

    with Pipeline(ctx.obj).lock():
        typer.echo(
            IndexBuilder(ctx.obj).build(
                activate=activate, mode=mode.value, source_id=source, kinds=tuple(kind) if kind else None
            )
        )


@app.command()
def ingest(ctx: typer.Context, videos: list[Path], activate: bool = False):
    """执行全部阶段；仅适用于服务可同时访问或已配置外部切换的环境。"""
    from .indexing import IndexBuilder
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj)
    with pipeline.lock():
        for path in videos:
            pipeline.prepare(path)
        pipeline.transcribe()
        pipeline.analyze()
        typer.echo(IndexBuilder(ctx.obj).build(activate=activate))


@app.command()
def search(ctx: typer.Context, text: str, granularity: str = "moment", limit: int = 20):
    from .search import SearchEngine
    from .types import TextSearch

    print_json(SearchEngine(ctx.obj).text(TextSearch(text=text, granularity=granularity, limit=limit)))


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
        store.activate(index_id)
    typer.echo(f"已发布索引 {index_id}；检索模型须使用该版本对应的配置")


@app.command()
def serve(ctx: typer.Context, host: str = "127.0.0.1", port: int = 8000):
    """启动页面和检索 API。"""
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(ctx.obj), host=host, port=port)


@app.command()
def evaluate(ctx: typer.Context, queries: Path, split: str = "test", output: Path | None = None):
    from .evaluation import evaluate as run_evaluation

    report = run_evaluation(ctx.obj, queries, split, output)
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
    weights_sha256: Annotated[str, typer.Option()],
    mmproj_sha256: Annotated[str, typer.Option()],
    quantization: Annotated[str, typer.Option()],
    output: Annotated[Path, typer.Option()],
    role: ModelRole = ModelRole.embedding,
):
    """固定已核验的文件哈希和当前运行身份；部署者需保证实际启动参数一致。"""
    import re

    from .e2e import save
    from .llama_cpp import inspect_service

    if output.exists():
        raise typer.BadParameter("清单已存在；更换模型请使用新文件和新实验")
    if not all(re.fullmatch(r"[0-9a-f]{64}", v) for v in (weights_sha256, mmproj_sha256)):
        raise typer.BadParameter("请提供 GGUF 与 mmproj 实际文件的 64 位小写 SHA256")
    if quantization in {"int8", "nf4", "unknown"} or not quantization.strip():
        raise typer.BadParameter("请填写实际 GGUF 量化，例如 Q8_0")
    service = getattr(ctx.obj, role.value)
    report = inspect_service(service)
    save(
        output,
        {
            "schema": 1,
            "role": role.value,
            "model": service.model,
            "weights_sha256": weights_sha256,
            "mmproj_sha256": mmproj_sha256,
            "quantization": quantization,
            "pooling": "last" if role == ModelRole.embedding else None,
            "video_fps": float(ctx.obj.video.fps),
            **report,
            "attestation": "Operator confirms file hashes, pooling last and server --video-fps. "
            "The standard API cannot independently verify these launch settings.",
        },
    )
    print_json({"manifest": str(output.resolve()), "runtime_id": report["runtime_id"]})


@e2e.command("phase")
def experiment_phase(
    ctx: typer.Context,
    phase: str,
    run: Annotated[Path, typer.Option()],
    python: Path | None = None,
    queries: Path | None = None,
    split: str = "test",
):
    """smoke → visual → preview → asr → align → analyze(-smoke) → enriched → evaluate/video-evaluate → check。"""
    from filelock import Timeout

    from .adapters import ModelError
    from .e2e import run_phase
    from .media import MediaError

    try:
        result = run_phase(ctx.obj, run, phase, python=python, queries=queries, split=split)
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


if __name__ == "__main__":
    app()
