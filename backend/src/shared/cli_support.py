"""Typer 命令共用的辅助：稳定排序的命令组与 JSON 输出。

放在 shared，供顶层 ``cli*.py`` 与 ``maintenance/backup_cli.py`` 共用，维护包因此不依赖 CLI 层。
"""

from __future__ import annotations

import json

import typer
from typer.core import TyperGroup


class SortedGroup(TyperGroup):
    """按字母顺序列出子命令，使各级 --help 的命令稳定排序。"""

    def list_commands(self, ctx):
        return sorted(super().list_commands(ctx))


def print_json(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
