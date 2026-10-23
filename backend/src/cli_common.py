"""CLI 命令共用的输出辅助。"""

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
