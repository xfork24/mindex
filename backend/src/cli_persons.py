"""`mindex persons` 命令组：人物库、参考图、处理与发布。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from mindex.cli_common import SortedGroup, print_json

app = typer.Typer(
    cls=SortedGroup,
    help="管理演员、作品、角色、参考图和人物检索发布。\n\n"
    "流程：save 登记实体 → reference 添加参考图 → process 扫描 → status 查看任务 → "
    "review 审核 → publish 发布；activate 回退已有检索组合。migrate-frames 迁移旧展示帧。\n\n"
    "reference/process 需要人物服务，任务后台执行。示例：mindex persons process --help。",
    no_args_is_help=True,
)


@app.command("list")
def persons_list(ctx: typer.Context):
    """列出人物库的实体与参考图登记信息。

    输出 entities、references 的 JSON；实体包括演员、作品和角色。无需人物服务，不执行识别或发布。

    示例：

    \b
      mindex persons list
    """
    from mindex.persons.persons import Persons
    from mindex.shared.storage import Store

    catalogue = Persons(Store(ctx.obj.data_dir))
    print_json({"entities": catalogue.entities(), "references": catalogue.references()})


@app.command("save")
def persons_save(
    ctx: typer.Context,
    file: Annotated[
        Path, typer.Argument(help="实体 JSON 文件；kind=actor/work/role，name 必填，角色需 work_ids。")
    ],
    identifier: Annotated[str | None, typer.Option(help="要修改的实体 ID；省略则新建。")] = None,
):
    """从 JSON 新建或修改演员、作品、角色实体。

    必填 kind（actor/work/role）与 name；可填 aliases、work_ids、actor_ids。角色至少关联一个 work_id。--identifier 指定修改对象，省略则新建；不会自动添加参考图或发布检索组合。

    示例：

    \b
      mindex persons save /路径/actor.json
    """
    from mindex.persons.person_models import Entity
    from mindex.persons.persons import Persons
    from mindex.shared.storage import Store

    print_json(
        Persons(Store(ctx.obj.data_dir)).save(Entity.model_validate_json(file.read_text()), identifier)
    )


@app.command("reference")
def persons_reference(
    ctx: typer.Context,
    actor: Annotated[str, typer.Argument(help="已登记演员实体 ID。")],
    images: Annotated[list[Path], typer.Argument(help="一张或多张本地参考图片；需要人物服务质检。")],
):
    """为指定演员添加一张或多张参考图并进行质量检查。

    ACTOR 使用人物库演员 ID，IMAGES 为本地图片路径。需要可用人物服务，质检失败或服务不可用则拒绝入库，输出参考图 JSON；原尺寸图像保存在人物库。

    示例：

    \b
      mindex persons reference ACTOR_ID /路径/正脸.png /路径/侧脸.jpg
    """
    from mindex.persons.person_processing import PersonClient, ensure_reference_quality
    from mindex.persons.persons import Persons
    from mindex.shared.storage import Store

    config = ctx.obj
    store = Store(config.data_dir)
    catalogue = Persons(store)
    client, space = PersonClient(config), None

    def check(item):
        nonlocal space
        space = ensure_reference_quality(store, config, client, item, space=space)

    print_json([catalogue.add_reference(actor, p.read_bytes(), checker=check) for p in images])


@app.command("process")
def persons_process(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="已登记素材 ID。")],
    start: Annotated[float, typer.Option(help="原片扫描开始时间，单位秒。")] = 0,
    end: Annotated[float | None, typer.Option(help="原片扫描结束时间，单位秒；省略处理至结尾。")] = None,
    publish: Annotated[bool, typer.Option(help="请求任务完成后发布人物检索组合；默认只处理。")] = False,
    retry: Annotated[
        str | None, typer.Option(help="同素材 failed/interrupted/cancelled 任务 ID，复用该任务重试。")
    ] = None,
):
    """登记人物扫描任务并启动独立后台进程，立即返回任务信息。

    SOURCE 是库内素材 ID，需人物服务与参考图；可限制原片起止秒数。输出 task_id、queued 状态和日志路径，使用 persons status 轮询。--retry 仅接受同素材 failed/interrupted/cancelled 任务 ID；默认不发布，--publish 请求完成后发布。

    示例：

    \b
      mindex persons process SOURCE_ID --start 0 --end 60
    """
    import subprocess
    import sys

    from mindex.persons.person_models import uid
    from mindex.persons.person_processing import PersonOptions
    from mindex.shared.processing_lock import ProcessingLock
    from mindex.shared.storage import Store, dumps

    store = Store(ctx.obj.data_dir)
    options = PersonOptions(start=start, end=end, publish=publish)
    if not store.one("SELECT id FROM sources WHERE id=?", (source,)):
        raise typer.BadParameter("找不到素材")
    with ProcessingLock(ctx.obj, source_id=source, work="persons"):
        identifier = retry or uid()
        if retry:
            from mindex.shared.task_cancel import registry_for
            from mindex.shared.task_progress import RETRYABLE

            row = store.one("SELECT * FROM person_tasks WHERE id=?", (retry,))
            if not row or row["status"] not in RETRYABLE or row["source_id"] != source:
                raise typer.BadParameter("重试任务必须失败/中断且归属该素材")
            registry_for(store.root).clear(retry)
            store.execute(
                "UPDATE person_tasks SET status='queued',error=NULL,result=NULL WHERE id=?", (retry,)
            )
        else:
            store.execute(
                "INSERT INTO person_tasks(id,source_id,status,config,options) VALUES(?,?,?,?,?)",
                (identifier, source, "queued", ctx.obj.model_dump_json(), dumps(options.model_dump())),
            )
    log_path = store.root / "persons" / "task-logs" / (identifier + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("ab") as handle:
            subprocess.Popen(
                [sys.executable, "-m", "mindex.persons.person_worker", str(store.root), identifier],
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                start_new_session=True,
            )
    except OSError as exc:
        store.execute("UPDATE person_tasks SET status='failed',error=? WHERE id=?", (str(exc), identifier))
        raise
    print_json({"task_id": identifier, "status": "queued", "log": str(log_path)})


@app.command("status")
def persons_status(
    ctx: typer.Context,
    task: Annotated[str, typer.Argument(help="人物后台任务 ID，来自 persons process 输出。")],
):
    """查看一个后台人物任务的状态、结果与错误。

    TASK 是 persons process 返回的任务 ID，不是素材 ID。读取已持久化记录并输出 JSON，不触发推理或重试。

    示例：

    \b
      mindex persons status TASK_ID
    """
    from mindex.persons.person_processing import PersonTasks
    from mindex.shared.storage import Store

    manager = PersonTasks.__new__(PersonTasks)
    manager.store = Store(ctx.obj.data_dir)
    print_json(manager.get(task))


@app.command("publish")
def persons_publish(
    ctx: typer.Context,
    content: Annotated[str | None, typer.Option(help="绑定的 ready 内容索引 ID；省略用活动索引。")] = None,
):
    """冻结当前已接受的人物证据并发布内容索引与人物快照的检索组合。

    默认绑定当前活动内容索引，或用 --content 指定 ready 索引。无需模型推理；未接受证据不会自动被批准。人物发布与内容建库独立，输出版本及组合信息。

    示例：

    \b
      mindex persons publish --content INDEX_ID
    """
    from mindex.persons.persons import Persons
    from mindex.shared.processing_lock import ProcessingLock, index_write_lock
    from mindex.shared.storage import Store

    store = Store(ctx.obj.data_dir)
    with ProcessingLock(ctx.obj), index_write_lock(store.root):
        print_json(Persons(store).publish(content))


@app.command("review")
def persons_review(
    ctx: typer.Context,
    file: Annotated[
        Path, typer.Argument(help="审核 JSON：source_id、shot_id、selections（actor_id 与可选 role_id）。")
    ],
):
    """从 JSON 人工确认一个镜头的出镜身份。

    输入包含 source_id、shot_id、selections；每个 selection 含 actor_id 与可选 role_id。写入审核结果，不调用识别服务；要用于已发布检索需再执行 persons publish。

    示例：

    \b
      mindex persons review /路径/review.json
    """
    from mindex.persons.person_models import ShotReview
    from mindex.persons.persons import Persons
    from mindex.shared.storage import Store

    print_json(Persons(Store(ctx.obj.data_dir)).shot_review(ShotReview.model_validate_json(file.read_text())))


@app.command("activate")
def persons_activate(
    ctx: typer.Context,
    release: Annotated[str, typer.Argument(help="已有检索组合 ID，包含内容索引和人物版本的组合。")],
):
    """激活已有内容索引与人物快照的检索组合。

    RELEASE 是检索组合 ID，不是单独的人物版本或内容索引 ID。组合必须存在且内容索引 ready、成员完整；会同时切换活动内容索引和检索组合指针。查询前应恢复匹配的嵌入配置。

    示例：

    \b
      mindex persons activate RELEASE_ID
    """
    from mindex.shared.storage import Store

    store = Store(ctx.obj.data_dir)
    store.activate_release(release)
    print_json({"release_id": release})


@app.command("migrate-frames")
def persons_migrate_frames(
    ctx: typer.Context,
    apply: Annotated[bool, typer.Option(help="实际写入或删除；缺省仅输出只读预览。")] = False,
    source: Annotated[str | None, typer.Option(help="素材 ID；省略处理全部素材，不是文件路径。")] = None,
):
    """迁移、压缩人物展示帧并清理冗余帧，默认只预览。

    原尺寸参考图和导入图保留；无需重新索引。--source 限定素材，--apply 才实际写入及清理。输出逐素材结果，任一素材出错则退出 1。

    示例：

    \b
      mindex persons migrate-frames --source SOURCE_ID --apply
    """
    from mindex.persons.persons import migrate_frames

    try:
        report = migrate_frames(ctx.obj, source_id=source, apply=apply)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    print_json(report)
    if any(row["error"] for row in report["sources"]):
        raise typer.Exit(1)
