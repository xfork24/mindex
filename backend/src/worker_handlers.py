"""Worker 任务处理器：队列任务 → 账本执行链（plans §7.2）。

账本（source_tasks/dialogue_tasks/person_tasks/topic_runs）是用户可见状态来源；
worker 领取队列任务后经对应 ``BackgroundTaskRunner`` 子类在当前线程执行（含工作
锁、准入、进度、终态），队列状态随后由 ``Worker`` 按原 owner+attempt 提交。
账本先完成、队列后完成：该窗口内崩溃会重派一个账本已完成的任务，执行体的
检查点/幂等语义保证至少一次交付下的正确性（阶段 5 以 outbox 收紧为同事务）。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

__all__ = ["register_ledger_handlers"]

# kind → (runner 构造器, 账本表)。处理器内按需构造 runner（依赖 runtime/store/
# embedder），避免 worker 启动即拉起全部子系统。
_KIND_TABLES = {
    "source": ("source_tasks", "mindex.admin.source_tasks", "SourceTasks"),
    "dialogue": ("dialogue_tasks", "mindex.admin.dialogue_tasks", "DialogueTasks"),
    "person": ("person_tasks", "mindex.persons.person_processing", "PersonTasks"),
    "topic": ("topic_runs", "mindex.topics.runner", "TopicRuns"),
}


def _build_runner(kind: str, config):
    import importlib

    from mindex.models.adapters import make_embedder
    from mindex.settings.runtime_settings import RuntimeConfig
    from mindex.shared.storage import Store

    _, module_name, class_name = _KIND_TABLES[kind]
    store = Store.open(config)
    runtime = RuntimeConfig(config, store)
    runner_class = getattr(importlib.import_module(module_name), class_name)
    # 四类账本 runner 均为 BackgroundTaskRunner(runtime, store, embedder) 形状；
    # TopicRuns 的 llm 缺省复用 writer（与 API 装配的缺省一致）。
    return runner_class(runtime, store, make_embedder(runtime.current()))


def register_ledger_handlers(handlers, config) -> None:
    for kind in _KIND_TABLES:
        handlers.register(kind, _make_handler(kind))


def _make_handler(kind: str):
    def handler(config, task):
        runner = _build_runner(kind, config)
        row = runner.row(task.ledger_id)
        restored = runner.restore(row)
        # 冻结的任务配置不含部署后端选择（database/vector/queue 为 exclude 字段，
        # 属节点级关注点）：以 worker 自身配置覆盖，防止回落本地 sqlite。
        restored = restored.model_copy(update={
            "database": config.database, "vector": config.vector, "queue": config.queue})
        lock = runner.row_lock(row)
        log.info("worker 执行 %s 任务 %s（attempt %s）", kind, task.ledger_id, task.attempt_id[:8])
        runner.run(task.ledger_id, restored, lock)

    return handler
