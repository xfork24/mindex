"""`mindex e2e` 命令组：单 GPU 分阶段真实验证。"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from filelock import Timeout

from shared.cli_support import SortedGroup, print_json
from shared.config import fingerprint

app = typer.Typer(
    cls=SortedGroup,
    help="单 GPU 分阶段真实验证；不会启动或切换远程模型。\n\n"
    "流程：init 初始化 → probe 探测 → pin 固定部署 → phase 分阶段验证；"
    "review 人工复核查询后 freeze 冻结标注，再 evaluate。status 查看进度。\n\n"
    "本组使用 YAML 原值，不合并设置页覆盖。示例：mindex e2e phase --help。",
    no_args_is_help=True,
)


class ModelRole(str, Enum):
    embedding = "embedding"
    asr = "asr"
    analyzer = "analyzer"


@app.command("init")
def init_experiment(
    ctx: typer.Context,
    source: Annotated[str, typer.Option(help="已完成分镜的素材 ID。")],
    run: Annotated[Path, typer.Option(help="实验目录，保存冻结输入、阶段状态和报告。")],
):
    """从已完成分镜的素材初始化真实验证实验。

        冻结分镜清单，生成待人工填写的标注草稿及查询视频，不调用模型。
    后续 probe/pin 固定部署，phase 分阶段执行，
    freeze 记录人工确认；--run 指定实验产物目录。

        示例：

        \b
          mindex e2e init --source SOURCE_ID --run /路径/实验
    """
    # 延迟导入：命令或模型工厂按需装配，保持帮助入口与可选模型环境隔离。
    from diagnostics.e2e import initialize
    from processing.pipeline import Pipeline

    with Pipeline(ctx.obj).lock():
        print_json(initialize(ctx.obj, source, run))


@app.command("probe")
def probe_deployment(
    ctx: typer.Context,
    output: Annotated[Path, typer.Option(help="兼容性报告 JSON 文件，失败时也写入。")],
    role: Annotated[
        ModelRole, typer.Option(help="探测或固定的模型角色，读取 YAML 中对应配置。")
    ] = ModelRole.embedding,
):
    """探测已启动的模型服务并保存兼容性报告，不执行推理。

        按 --role 读取 YAML 的服务配置，不启动或切换服务。报告含 ready、端点及兼容信息；探测失败也保存 JSON，
    并以退出码 1 结束。此结果不等于推理质量或能力验收。

        示例：

        \b
          mindex e2e probe --role embedding --output /路径/probe.json
    """
    # 延迟导入：当前模块存在同名绑定，保留局部名称语义。
    from models.llama_cpp import ModelError, inspect_service

    # 延迟导入：命令或模型工厂按需装配，保持帮助入口与可选模型环境隔离。
    from shared.jsonio import save

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


@app.command("pin")
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

        先启动对应服务。两种方式互斥：本地同时提供 weights-file、mmproj-file、server-cwd 并计算 SHA256；
    远程同时声明 weights-sha256、mmproj-sha256。可保存部署者确认的启动参数，远程摘要只代表操作员声明。
    已有输出文件会拒绝覆盖，换部署使用新文件；后续仍需 smoke 验证。

        示例：

        \b
          mindex e2e pin --quantization Q8_0 --weights-file /模型/model.gguf --mmproj-file /模型/mmproj.gguf
    --server-cwd /服务目录 --output /路径/deployment.json
    """
    # 延迟导入：当前模块存在同名绑定，保留局部名称语义。
    from models.adapters import ModelError

    # 延迟导入：命令或模型工厂按需装配，保持帮助入口与可选模型环境隔离。
    from models.deployment import create_manifest
    from models.llama_cpp import inspect_service
    from shared.jsonio import save

    if output.exists():
        raise typer.BadParameter("清单已存在；更换部署请使用新文件，保留旧清单")
    try:
        parameters = json.loads(launch_parameters.read_text(encoding="utf-8")) if launch_parameters else {}
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


@app.command("phase")
def experiment_phase(
    ctx: typer.Context,
    phase: Annotated[
        str,
        typer.Argument(
            help=(
                "smoke/visual/preview/asr/align/analyze-smoke/analyze/enriched/evaluate/video-evaluate/chec"
                "k。"
            )
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

        PHASE 可选 smoke、visual、preview、asr、align、analyze-smoke、analyze、enriched、evaluate、
    video-evaluate、check。align 需 --python；preview 需 --queries 草稿；evaluate/video-evaluate 需 freeze
    后的标注。构建阶段默认不发布，查询默认使用实验记录的索引。video-evaluate 的默认 test 分组会映射为 video。
    不会自动启动或切换服务，按阶段准备模型；失败输出结构化错误并退出 1。

        示例：

        \b
          mindex e2e phase visual --run /路径/实验
    """

    # 延迟导入：命令或模型工厂按需装配，保持帮助入口与可选模型环境隔离。
    from diagnostics.e2e import run_phase

    # 延迟导入：当前模块存在同名绑定，保留局部名称语义。
    from models.adapters import ModelError

    # 延迟导入：命令或模型工厂按需装配，保持帮助入口与可选模型环境隔离。
    from shared.media import MediaError

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


@app.command("freeze")
def freeze_labels(
    queries: Annotated[Path, typer.Argument(help="查询标注 JSONL 文件路径。")],
    run: Annotated[Path, typer.Option(help="实验目录，保存冻结输入、阶段状态和报告。")],
    confirmed_by: Annotated[str, typer.Option(help="实际完成人工复核的姓名或标识，将写入冻结清单。")],
):
    """记录人工确认者并冻结查询标注，生成 SHA256 清单。

    先逐条复核查询与目标；此命令不会替用户判断标注正确性。冻结后修改查询文件需重新确认冻结，
    否则评测拒绝不匹配的摘要。

    示例：

    \b
      mindex e2e freeze /路径/queries.jsonl --run /路径/实验 --confirmed-by '姓名'
    """
    # 延迟导入：命令或模型工厂按需装配，保持帮助入口与可选模型环境隔离。
    from diagnostics.e2e import freeze

    print_json({"frozen": freeze(run, queries, confirmed_by)})


@app.command("review")
def review_labels(
    ctx: typer.Context,
    queries: Annotated[Path, typer.Argument(help="查询标注 JSONL 文件路径。")],
    run: Annotated[Path, typer.Option(help="实验目录，保存冻结输入、阶段状态和报告。")],
):
    """生成可播放候选并导出标注的本地复核页面。

        读取实验与查询文件，输出 review_page 路径；在浏览器中人工查看并选择目标。
    不会自动确认标签，最终评测前仍需
    freeze。

        示例：

        \b
          mindex e2e review /路径/queries.jsonl --run /路径/实验
    """
    # 延迟导入：命令或模型工厂按需装配，保持帮助入口与可选模型环境隔离。
    from diagnostics.label_review import render_review

    print_json({"review_page": str(render_review(ctx.obj, run.resolve(), queries))})


@app.command("status")
def experiment_status(
    ctx: typer.Context, run: Annotated[Path, typer.Option(help="实验目录，保存冻结输入、阶段状态和报告。")]
):
    """查看实验阶段状态、活动索引和当前配置的分析完成数量。

    读取 --run 的状态与素材库，不请求模型或推进处理阶段，输出 JSON。

    示例：

    \b
      mindex e2e status --run /路径/实验
    """
    # 延迟导入：命令或模型工厂按需装配，保持帮助入口与可选模型环境隔离。
    from diagnostics.e2e import progress

    print_json(progress(ctx.obj, run))
