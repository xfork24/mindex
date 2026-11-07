"""`mindex clips` 命令组：旧切片与 shot 媒体引用维护。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from shared.cli_support import SortedGroup, print_json

app = typer.Typer(
    cls=SortedGroup,
    help="维护旧切片与 shot 媒体引用，使用 YAML 原配置。\n\n"
    "migrate 盘点/迁移，verify-archives 校验归档原片，repair-boundaries 诊断/修复切点；"
    "验收迁移后 prune 删除旧实体，materialize --all 从 shot 恢复实体。\n\n"
    "migrate、repair-boundaries、prune 默认预览；materialize --all 直接写文件。"
    "通用磁盘回收用 mindex clean。示例：mindex clips migrate --help。",
    no_args_is_help=True,
)


@app.command("migrate")
def clips_migrate(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Option(help="素材 ID；省略处理全部素材，不是文件路径。")] = None,
    apply: Annotated[bool, typer.Option(help="实际写入或删除；缺省仅输出只读预览。")] = False,
    verify: Annotated[
        bool, typer.Option(help="与 --apply 配合验证迁移媒体；仅预览时不执行媒体验证。")
    ] = False,
):
    """盘点旧切片并迁移为 shot 媒体引用，默认只读。

        --apply 备份并登记引用；--verify 与 --apply 配合验证媒体，生成后续 prune 使用的迁移报告。
    此命令不物理删除旧实体。省略 --source 处理全部素材，使用 YAML 原配置而非设置页覆盖层。

        示例：

        \b
          mindex clips migrate --source SOURCE_ID --apply --verify
    """
    # 延迟导入：命令或模型工厂按需装配，保持帮助入口与可选模型环境隔离。
    from maintenance.clips import migrate

    print_json(migrate(ctx.obj, source, apply=apply, verify=verify))


@app.command("materialize")
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
    # 延迟导入：命令或模型工厂按需装配，保持帮助入口与可选模型环境隔离。
    from maintenance.clips import materialize_all

    print_json(materialize_all(ctx.obj))


@app.command("verify-archives")
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
    # 延迟导入：命令或模型工厂按需装配，保持帮助入口与可选模型环境隔离。
    from maintenance.clips import verify_archives

    print_json(verify_archives(ctx.obj, source))


@app.command("repair-boundaries")
def clips_repair_boundaries(
    ctx: typer.Context,
    source: Annotated[str | None, typer.Option(help="素材 ID；省略处理全部素材，不是文件路径。")] = None,
    audit_file: Annotated[
        Path | None, typer.Option(help="读取已有审计 JSON；省略则根据 --source 现场诊断。")
    ] = None,
    apply: Annotated[bool, typer.Option(help="实际写入或删除；缺省仅输出只读预览。")] = False,
):
    """诊断原片中的镜头切点并按审计报告修复，默认只读预览。

        可按 --source 生成诊断，也可用 --audit-file 读取已有 JSON；
    --apply 才执行修复，保留旧媒体。应先检查报告，
    避免直接对生产素材试运行修复。

        示例：

        \b
          mindex clips repair-boundaries --source SOURCE_ID
    """
    # 延迟导入：命令或模型工厂按需装配，保持帮助入口与可选模型环境隔离。
    from processing.shot_repair import apply as repair
    from processing.shot_repair import audit
    from shared.storage import Store

    report = (
        json.loads(audit_file.read_text(encoding="utf-8"))
        if audit_file
        else audit(Store.open(ctx.obj), source)
    )
    print_json(repair(ctx.obj, report) if apply else report)


@app.command("prune")
def clips_prune(
    ctx: typer.Context,
    migration_report: Annotated[Path, typer.Argument(help="已通过验证的迁移报告 JSON 路径。")],
    apply: Annotated[bool, typer.Option(help="实际写入或删除；缺省仅输出只读预览。")] = False,
):
    """根据已验证的迁移报告预览或删除旧切片实体。

        MIGRATION_REPORT 是 clips migrate --apply --verify 生成的报告文件。先验收引用与播放，再加 --apply
    物理删除；保留原片与 shot，不作为通用磁盘清理入口。

        示例：

        \b
          mindex clips prune /路径/migration-report.json
    """
    # 延迟导入：命令或模型工厂按需装配，保持帮助入口与可选模型环境隔离。
    from maintenance.clips import prune

    print_json(prune(ctx.obj, migration_report, apply=apply))
