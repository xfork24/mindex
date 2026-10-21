"""中文备份 CLI；业务实现放在 backup 模块。"""

from __future__ import annotations

import json
import sqlite3
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help='离线数据备份与迁移（请先停止 Web 和全部后台写入任务）', no_args_is_help=True)


class Verification(StrEnum):
    full = 'full'
    basic = 'basic'


def output(value, structured):
    if structured:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
        return
    for key, label in (
        ('destination', '备份目录'), ('target', '恢复目录'), ('version', '备份版本'),
        ('bytes', '总字节数'), ('files', '文件数'), ('includes_secrets', '包含已存密钥'),
        ('config_action', '配置处理'), ('active_index', '活动索引'),
        ('dependencies', '依赖'),
        ('searchable_sources', '可检索素材'), ('required_environment', '需补充的环境变量'),
        ('missing_dependencies', '需补充的部署依赖'), ('skipped_sources', '已跳过素材'),
        ('excluded_jobs', '排除的创作任务'), ('id_maps', '版本映射'),
        ('legacy_representation_sources', '本次允许旧表示兼容的素材'),
        ('integer_id_maps', '本地整数 ID 映射'), ('config_diff', '配置差异'), ('result', '处理结果'),
        ('verification', '校验模式'), ('skipped_checks', '省略的复核'),
    ):
        if key in value:
            typer.echo(f'{label}：{json.dumps(value[key], ensure_ascii=False)}')
    if 'sources' in value:
        typer.echo(f'素材（{len(value["sources"])}）：')
        for row in value['sources']:
            typer.echo(f'  {row["id"]}  {row["name"]}')
    if 'collections' in value:
        typer.echo('索引：')
        for row in value['collections']:
            typer.echo(f'  {row["index_id"]}  {row["count"]} 个向量')
    if value.get('dry_run'):
        typer.echo('预演完成，未写入目标备份、数据或配置。')
    elif value.get('ok'):
        typer.echo('操作完成。')


def call(structured, function, *args, **kwargs):
    try:
        value = function(*args, **kwargs)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        if structured:
            typer.echo(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        else:
            typer.echo(f'备份操作失败：{exc}', err=True)
        raise typer.Exit(1) from None
    output(value, structured)


def choose(available, all_sources, sources):
    if all_sources or sources:
        return sources
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('非交互环境请指定 --all 或重复使用 --source 素材ID')
    selected = set()
    while True:
        query = typer.prompt('搜索素材名称或 ID（空值显示全部；输入 /完成 结束选择）', default='')
        if query == '/完成':
            if selected:
                return sorted(selected)
            typer.echo('请至少选择一个素材。')
            continue
        visible = [r for r in available if query.casefold() in (r['id'] + ' ' + r['name']).casefold()]
        for number, row in enumerate(visible, 1):
            typer.echo(f'{number}. [{"✓" if row["id"] in selected else " "}] {row["name"]}  {row["id"]}')
        answer = typer.prompt('输入多个序号（逗号分隔）切换勾选，或按回车继续搜索', default='')
        if not answer:
            continue
        try:
            numbers = [int(token.strip()) for token in answer.replace('，', ',').split(',')]
            if any(number < 1 or number > len(visible) for number in numbers):
                raise ValueError
        except ValueError:
            typer.echo('序号无效，请重试。')
            continue
        for number in numbers:
            identifier = visible[number - 1]['id']
            if identifier in selected:
                selected.remove(identifier)
            else:
                selected.add(identifier)
        typer.echo(f'已选择 {len(selected)} 个素材。')


@app.command('list')
def list_sources(ctx: typer.Context, structured: Annotated[bool, typer.Option('--json', help='输出 JSON')] = False):
    """列出当前库可备份的素材。"""
    from mindex.maintenance.backup import source_list

    call(structured, lambda: {'sources': source_list(ctx.obj.data_dir)})


@app.command('inspect')
def inspect(directory: Path, structured: Annotated[bool, typer.Option('--json', help='输出 JSON')] = False):
    """校验备份并查看素材、索引、配置与密钥包含情况。"""
    from mindex.maintenance.backup import inspect_bundle, summary

    call(structured, lambda: summary(inspect_bundle(directory)))


@app.command('export')
def export(
    ctx: typer.Context, directory: Path,
    all_sources: Annotated[bool, typer.Option('--all', help='备份整个库')] = False,
    sources: Annotated[list[str] | None, typer.Option('--source', help='素材 ID，可重复指定')] = None,
    include_secrets: Annotated[bool, typer.Option('--include-secrets', help='包含设置页保存的密钥')] = False,
    allow_legacy_representations: Annotated[bool, typer.Option('--allow-legacy-representations',
        help='仅单素材临时兼容 v2 迁移遗留表示；默认严格检查')] = False,
    dry_run: Annotated[bool, typer.Option('--dry-run', help='验证并预演，不生成目标备份')] = False,
    verification: Annotated[Verification, typer.Option('--verification',
        help='full 完整校验；basic 仅基础校验，供信任备份内容的用户主动选择')] = Verification.full,
    structured: Annotated[bool, typer.Option('--json', help='输出 JSON')] = False,
):
    """导出一致快照；默认排除密钥，不读取环境变量中的密钥。"""
    from mindex.maintenance.backup import export_backup, source_list

    def run():
        picked = choose(source_list(ctx.obj.data_dir), all_sources, sources)
        return export_backup(ctx.obj, directory, all_sources=all_sources, sources=picked,
                             include_secrets=include_secrets, dry_run=dry_run,
                             allow_legacy_representations=allow_legacy_representations,
                             verification=verification.value,
                             config_path=ctx.meta['config_path'], progress=lambda message: typer.echo(message, err=True))
    call(structured, run)


@app.command('import')
def import_(
    ctx: typer.Context, directory: Path,
    all_sources: Annotated[bool, typer.Option('--all', help='导入整个备份')] = False,
    sources: Annotated[list[str] | None, typer.Option('--source', help='素材 ID，可重复指定')] = None,
    data_dir: Annotated[Path | None, typer.Option('--data-dir', help='目标数据目录')] = None,
    skip_existing: Annotated[bool, typer.Option('--skip-existing', help='保留重复素材及其全部衍生数据')] = False,
    restore_config: Annotated[bool, typer.Option('--restore-config', help='显式恢复已有库的系统配置')] = False,
    dry_run: Annotated[bool, typer.Option('--dry-run', help='按所选模式验证，不切换目标库')] = False,
    verification: Annotated[Verification, typer.Option('--verification',
        help='full 完整校验；basic 省去全文件哈希复算、逐向量复核及分镜探测')] = Verification.full,
    structured: Annotated[bool, typer.Option('--json', help='输出 JSON')] = False,
):
    """空库恢复或已有库合并；所有向量直接复用，不调用模型。"""
    from mindex.maintenance.backup import import_backup, inspect_bundle

    def run():
        picked = (sources if verification == Verification.basic and (all_sources or sources)
                  else choose(inspect_bundle(directory, verification=verification.value)['sources'], all_sources, sources))
        config = ctx.obj.model_copy(update={'data_dir': data_dir.resolve()}) if data_dir else ctx.obj
        return import_backup(config, directory, all_sources=all_sources, sources=picked,
                             skip_existing=skip_existing, restore_config=restore_config, dry_run=dry_run,
                             verification=verification.value,
                             config_path=ctx.meta['config_path'], progress=lambda message: typer.echo(message, err=True))
    call(structured, run)


@app.command('recover')
def recover(
    ctx: typer.Context,
    data_dir: Annotated[Path | None, typer.Option('--data-dir', help='中断迁移的数据目录')] = None,
    structured: Annotated[bool, typer.Option('--json', help='输出 JSON')] = False,
):
    """按持久化日志回滚未完成的目录切换，或清理已成功切换的旧目录。"""
    from mindex.maintenance.backup import recover_import

    call(structured, recover_import, (data_dir or ctx.obj.data_dir).resolve(), config_path=ctx.meta['config_path'])
