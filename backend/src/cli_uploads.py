"""CLI 上传会话与 REST 共用持久状态机；令牌只从环境变量读取。"""
from __future__ import annotations

import hashlib
import os
from functools import wraps
from pathlib import Path
from typing import Annotated

import typer

from mindex.shared.cli_support import SortedGroup, print_json
from mindex.shared.storage import Store
from mindex.shared.upload_sessions import UploadSessions

app = typer.Typer(cls=SortedGroup, help="管理分片上传会话（create、part、complete、abort、show、reap）。示例：mindex uploads show SESSION_ID。", no_args_is_help=True)


def checked(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, OSError, RuntimeError) as exc:
            typer.echo(f"上传会话操作失败：{exc}", err=True)
            raise typer.Exit(1) from exc
    return run


def service(ctx):
    while ctx.obj is None and ctx.parent is not None:
        ctx = ctx.parent
    return UploadSessions(Store.open(ctx.obj), ctx.obj)


def token(env):
    value = os.environ.get(env, "")
    if not value:
        raise typer.BadParameter(f"请在环境变量 {env} 提供上传会话凭据")
    return value


@app.command()
@checked
def create(ctx: typer.Context, filename: Annotated[str, typer.Argument(help="原始文件名。")],
           size: Annotated[int, typer.Argument(help="完整文件的字节数。")],
           sha256: Annotated[str, typer.Argument(help="完整文件的 64 位十六进制 SHA256。") ]):
    """声明文件大小和 SHA256，返回会话与上传凭据。

    示例：mindex uploads create clip.mp4 1024 SHA256（把 SHA256 替换为文件摘要）。
    """
    print_json(service(ctx).create(filename, size, sha256))


@app.command()
@checked
def part(ctx: typer.Context, session_id: Annotated[str, typer.Argument(help="create 返回的会话 ID。") ], number: Annotated[int, typer.Argument(help="分片编号，从 1 开始。")],
         path: Annotated[Path, typer.Argument(help="分片文件的本机路径。") ],
         token_env: Annotated[str, typer.Option(help="保存会话凭据的环境变量名。") ] = "MINDEX_UPLOAD_TOKEN"):
    """提交一个分片文件，编号从 1 开始；可重复提交同摘要分片。

    示例：mindex uploads part SESSION_ID 1 chunk.bin。
    """
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        source.seek(0)
        print_json(service(ctx).part(session_id, token(token_env), number, source, path.stat().st_size, digest))


@app.command()
@checked
def show(ctx: typer.Context, session_id: Annotated[str, typer.Argument(help="create 返回的会话 ID。") ], token_env: Annotated[str, typer.Option(help="保存会话凭据的环境变量名。") ] = "MINDEX_UPLOAD_TOKEN"):
    """读取已登记分片与上传进度。

    示例：mindex uploads show SESSION_ID。
    """
    print_json(service(ctx).get(session_id, token(token_env)))


@app.command()
@checked
def complete(ctx: typer.Context, session_id: Annotated[str, typer.Argument(help="create 返回的会话 ID。") ], token_env: Annotated[str, typer.Option(help="保存会话凭据的环境变量名。") ] = "MINDEX_UPLOAD_TOKEN"):
    """复算完整文件摘要，原子登记正式素材；重复完成返回同一结果。

    示例：mindex uploads complete SESSION_ID。
    """
    print_json(service(ctx).complete(session_id, token(token_env)))


@app.command()
@checked
def abort(ctx: typer.Context, session_id: Annotated[str, typer.Argument(help="create 返回的会话 ID。") ], token_env: Annotated[str, typer.Option(help="保存会话凭据的环境变量名。") ] = "MINDEX_UPLOAD_TOKEN"):
    """取消未完成会话并回收 pending 分片。

    示例：mindex uploads abort SESSION_ID。
    """
    print_json(service(ctx).abort(session_id, token(token_env)))


@app.command()
@checked
def reap(ctx: typer.Context):
    """回收过期会话及已终止会话的 pending 分片。

    示例：mindex uploads reap。
    """
    print_json({"reaped": service(ctx).reap()})
