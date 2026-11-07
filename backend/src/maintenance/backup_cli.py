"""中文备份 CLI；业务实现放在 backup 模块。"""

from __future__ import annotations

import json
import sqlite3
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from shared.cli_support import SortedGroup

app = typer.Typer(
    cls=SortedGroup,
    help="离线数据备份与迁移；先停止 Web 和全部后台写入任务。\n\n"
    "list 查看素材，export 导出一致快照，inspect 校验备份，import 恢复或合并，recover 恢复中断切换。"
    "全部复用既有向量，不调用模型；使用 YAML 原配置，基础配置与数据库覆盖层分别保存。\n\n"
    "导入导出先用 --dry-run 预演。非交互环境必须 --all 或重复 --source；"
    "默认 full 校验，basic 会省略部分损坏检测。示例：mindex backup export --help。",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
)


class Verification(StrEnum):
    full = "full"
    basic = "basic"


def output(value, structured):
    if structured:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
        return
    for key, label in (
        ("destination", "备份目录"),
        ("target", "恢复目录"),
        ("version", "备份版本"),
        ("bytes", "总字节数"),
        ("files", "文件数"),
        ("includes_secrets", "包含已存密钥"),
        ("config_action", "配置处理"),
        ("active_index", "活动索引"),
        ("dependencies", "依赖"),
        ("searchable_sources", "可检索素材"),
        ("required_environment", "需补充的环境变量"),
        ("missing_dependencies", "需补充的部署依赖"),
        ("skipped_sources", "已跳过素材"),
        ("excluded_jobs", "排除的创作任务"),
        ("id_maps", "版本映射"),
        ("legacy_representation_sources", "本次允许旧表示兼容的素材"),
        ("integer_id_maps", "本地整数 ID 映射"),
        ("config_diff", "配置差异"),
        ("result", "处理结果"),
        ("verification", "校验模式"),
        ("skipped_checks", "省略的复核"),
    ):
        if key in value:
            typer.echo(f"{label}：{json.dumps(value[key], ensure_ascii=False)}")
    if "sources" in value:
        typer.echo(f"素材（{len(value['sources'])}）：")
        for row in value["sources"]:
            typer.echo(f"  {row['id']}  {row['name']}")
    if "collections" in value:
        typer.echo("索引：")
        for row in value["collections"]:
            typer.echo(f"  {row['index_id']}  {row['count']} 个向量")
    if value.get("dry_run"):
        typer.echo("预演完成，未写入目标备份、数据或配置。")
    elif value.get("ok"):
        typer.echo("操作完成。")


def call(structured, function, *args, **kwargs):
    try:
        value = function(*args, **kwargs)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        if structured:
            typer.echo(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            typer.echo(f"备份操作失败：{exc}", err=True)
        raise typer.Exit(1) from None
    output(value, structured)


def choose(available, all_sources, sources):
    if all_sources or sources:
        return sources
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("非交互环境请指定 --all 或重复使用 --source 素材ID")
    selected = set()
    while True:
        query = typer.prompt("搜索素材名称或 ID（空值显示全部；输入 /完成 结束选择）", default="")
        if query == "/完成":
            if selected:
                return sorted(selected)
            typer.echo("请至少选择一个素材。")
            continue
        visible = [r for r in available if query.casefold() in (r["id"] + " " + r["name"]).casefold()]
        for number, row in enumerate(visible, 1):
            typer.echo(f"{number}. [{'✓' if row['id'] in selected else ' '}] {row['name']}  {row['id']}")
        answer = typer.prompt("输入多个序号（逗号分隔）切换勾选，或按回车继续搜索", default="")
        if not answer:
            continue
        try:
            numbers = [int(token.strip()) for token in answer.replace("，", ",").split(",")]
            if any(number < 1 or number > len(visible) for number in numbers):
                raise ValueError
        except ValueError:
            typer.echo("序号无效，请重试。")
            continue
        for number in numbers:
            identifier = visible[number - 1]["id"]
            if identifier in selected:
                selected.remove(identifier)
            else:
                selected.add(identifier)
        typer.echo(f"已选择 {len(selected)} 个素材。")


@app.command("list")
def list_sources(
    ctx: typer.Context,
    structured: Annotated[
        bool, typer.Option("--json", help="将结果以 JSON 写到标准输出；备份进度写到标准错误。")
    ] = False,
):
    """列出当前素材库可备份的素材。

    默认中文可读输出，--json 供脚本使用；不导出数据、不调用模型。

    示例：

    \b
      mindex backup list --json
    """
    from maintenance.backup_files import source_list

    call(structured, lambda: {"sources": source_list(ctx.obj.data_dir)})


@app.command("inspect")
def inspect(
    directory: Annotated[Path, typer.Argument(help="备份根目录路径。")],
    structured: Annotated[
        bool, typer.Option("--json", help="将结果以 JSON 写到标准输出；备份进度写到标准错误。")
    ] = False,
):
    """完整校验备份目录并查看素材、索引、配置和密钥包含情况。

        读取清单及文件 SHA256，大备份可能耗时；不导入、不调用模型。DIRECTORY 是备份根目录而非 data 子目录，
    此命令没有 --verification basic 快捷模式。

        示例：

        \b
          mindex backup inspect /备份/mindex --json
    """
    from maintenance.backup import inspect_bundle, summary

    call(structured, lambda: summary(inspect_bundle(directory)))


@app.command("export")
def export(
    ctx: typer.Context,
    directory: Annotated[Path, typer.Argument(help="新备份目录，必须不存在。")],
    all_sources: Annotated[bool, typer.Option("--all", help="选择全部素材；与 --source 互斥。")] = False,
    sources: Annotated[
        list[str] | None, typer.Option("--source", help="素材 ID，可重复指定 --source ID；与 --all 互斥。")
    ] = None,
    include_secrets: Annotated[
        bool,
        typer.Option(
            "--include-secrets", help="将设置页保存的明文密钥写入备份；默认移除，始终不读取环境变量密钥。"
        ),
    ] = False,
    allow_legacy_representations: Annotated[
        bool,
        typer.Option(
            "--allow-legacy-representations",
            help="仅单素材临时兼容旧版 v2 迁移遗留表示，不适用于全库/多素材。",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="构建并校验临时快照；不发布目标数据或配置，仍需时间与暂存空间。")
    ] = False,
    verification: Annotated[
        Verification,
        typer.Option(
            "--verification",
            help="full 完整校验；basic 省略部分哈希/向量/媒体复核，仅在信任数据完整性时主动选择。",
        ),
    ] = Verification.full,
    structured: Annotated[
        bool, typer.Option("--json", help="将结果以 JSON 写到标准输出；备份进度写到标准错误。")
    ] = False,
):
    """导出离线一致快照，默认排除密钥且不读取环境变量密钥。

        先停止 Web 和全部后台写入任务，目标目录必须不存在。--all 全库，--source 可重复选择；两者互斥。
    省略选择仅在交互终端提供搜索多选，脚本必须显式选择。向量经 API 导出，不重新推理；
    权重和可下载模型缓存不在备份内。--dry-run 实际构建临时快照后删除，仍需时间与空间；--include-secrets
    会包含已存明文凭据。

        示例：

        \b
          mindex backup export /备份/mindex --all --dry-run
    """
    from maintenance.backup import export_backup
    from maintenance.backup_files import source_list

    def run():
        picked = choose(source_list(ctx.obj.data_dir), all_sources, sources)
        return export_backup(
            ctx.obj,
            directory,
            all_sources=all_sources,
            sources=picked,
            include_secrets=include_secrets,
            dry_run=dry_run,
            allow_legacy_representations=allow_legacy_representations,
            verification=verification.value,
            config_path=ctx.meta["config_path"],
            progress=lambda message: typer.echo(message, err=True),
        )

    call(structured, run)


@app.command("import")
def import_(
    ctx: typer.Context,
    directory: Annotated[Path, typer.Argument(help="待导入备份的根目录。")],
    all_sources: Annotated[bool, typer.Option("--all", help="选择全部素材；与 --source 互斥。")] = False,
    sources: Annotated[
        list[str] | None, typer.Option("--source", help="素材 ID，可重复指定 --source ID；与 --all 互斥。")
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="目标数据目录，按当前工作目录解析；优先于全局 --data-dir。"),
    ] = None,
    skip_existing: Annotated[
        bool, typer.Option("--skip-existing", help="保留目标库重复素材及全部衍生数据；默认遇重复素材报错。")
    ] = False,
    restore_config: Annotated[
        bool,
        typer.Option("--restore-config", help="已有库也恢复备份系统配置；默认保留目标配置，空库自动恢复。"),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="构建并校验临时快照；不发布目标数据或配置，仍需时间与暂存空间。")
    ] = False,
    verification: Annotated[
        Verification,
        typer.Option(
            "--verification",
            help="full 完整校验；basic 省略部分哈希/向量/媒体复核，仅在信任数据完整性时主动选择。",
        ),
    ] = Verification.full,
    structured: Annotated[
        bool, typer.Option("--json", help="将结果以 JSON 写到标准输出；备份进度写到标准错误。")
    ] = False,
):
    """从备份恢复空库或合并已有库，复用向量，不调用模型。

        先停止 Web 和全部后台写入任务。空库恢复基础配置与活动组合，允许目标 YAML 尚不存在；
    已有库默认保留配置，
    --restore-config 才恢复。重复素材默认失败，--skip-existing 保留目标及其衍生数据；兼容空间才合并活动索引。
    --all 与 --source 互斥，脚本必须显式选择。--dry-run 构建并校验暂存数据，不切换目标；默认 full 校验，basic
    省略部分损坏检测。中断后保留日志并使用 backup recover。

        示例：

        \b
          mindex backup import /备份/mindex --all --data-dir /新路径/data --dry-run
    """
    from maintenance.backup import import_backup, inspect_bundle

    def run():
        picked = (
            sources
            if verification == Verification.basic and (all_sources or sources)
            else choose(
                inspect_bundle(directory, verification=verification.value)["sources"], all_sources, sources
            )
        )
        config = ctx.obj.model_copy(update={"data_dir": data_dir.resolve()}) if data_dir else ctx.obj
        return import_backup(
            config,
            directory,
            all_sources=all_sources,
            sources=picked,
            skip_existing=skip_existing,
            restore_config=restore_config,
            dry_run=dry_run,
            verification=verification.value,
            config_path=ctx.meta["config_path"],
            progress=lambda message: typer.echo(message, err=True),
        )

    call(structured, run)


@app.command("recover")
def recover(
    ctx: typer.Context,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="中断迁移的原目标数据目录；省略使用当前配置 data_dir。")
    ] = None,
    structured: Annotated[
        bool, typer.Option("--json", help="将结果以 JSON 写到标准输出；备份进度写到标准错误。")
    ] = False,
):
    """根据持久化迁移日志恢复中断的导入切换。

    停止全部写入，使用原配置与原目标数据路径。未提交操作回滚旧数据与配置；已提交操作清理旧目录。不要先手动删除迁移日志或旧副本。输出恢复结果，不调用模型。

    示例：

    \b
      mindex --config /路径/config.local.yaml backup recover --data-dir /路径/data --json
    """
    from maintenance.backup import recover_import

    call(
        structured,
        recover_import,
        (data_dir or ctx.obj.data_dir).resolve(),
        config_path=ctx.meta["config_path"],
    )
