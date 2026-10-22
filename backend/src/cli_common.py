"""CLI 命令共用的输出辅助。"""

from __future__ import annotations

import json

import typer


def print_json(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
