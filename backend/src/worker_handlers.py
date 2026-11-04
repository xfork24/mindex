"""Worker 任务处理器：队列任务 → 账本执行链（plans §7.2）。

账本（source_tasks/dialogue_tasks/person_tasks/topic_runs/index_tasks）是用户可见状态来源；
worker 领取队列任务后经对应 ``BackgroundTaskRunner`` 子类在当前线程执行（含工作
锁、准入、进度、终态），队列状态随后由 ``Worker`` 按原 owner+attempt 提交。
队列运行期间由 task_progress 将账本终态与队列终态同事务提交，并校验当前 attempt；
中间业务写事务与处理线程同样携带 fencing 凭证。
"""

from __future__ import annotations

import logging
import time

from filelock import Timeout

log = logging.getLogger(__name__)

__all__ = ["register_ledger_handlers"]

# kind → (runner 构造器, 账本表)。处理器内按需构造 runner（依赖 runtime/store/
# embedder），避免 worker 启动即拉起全部子系统。
_KIND_TABLES = {
    "source": ("source_tasks", "mindex.admin.source_tasks", "SourceTasks"),
    "dialogue": ("dialogue_tasks", "mindex.admin.dialogue_tasks", "DialogueTasks"),
    "person": ("person_tasks", "mindex.persons.person_processing", "PersonTasks"),
    "topic": ("topic_runs", "mindex.topics.runner", "TopicRuns"),
    "index": ("index_tasks", "mindex.admin.source_tasks", "IndexTasks"),
}


def _build_runner(kind: str, config):
    import importlib

    from mindex.settings.runtime_settings import RuntimeConfig
    from mindex.shared.storage import Store

    _, module_name, class_name = _KIND_TABLES[kind]
    store = Store.open(config)
    runtime = RuntimeConfig(config, store)
    runner_class = getattr(importlib.import_module(module_name), class_name)
    # 各类账本 runner 均为 BackgroundTaskRunner(runtime, store, embedder) 形状；
    # TopicRuns 的 llm 缺省复用 writer（与 API 装配的缺省一致）。
    return runner_class(runtime, store)


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
            key: getattr(config, key) for key in
            ("database", "vector", "storage", "locks", "queue", "gpu", "server", "data_dir")})
        # 先恢复账本中的模型空间，再创建客户端；排队后热更新不能悄悄替换任务模型。
        if runner.embedder is None:
            from mindex.models.adapters import make_embedder

            runner.embedder = make_embedder(restored)
        lock = runner.row_lock(row)
        while True:
            task.execution.attempt.check()
            try:
                lock.acquire(timeout=0)
                break
            except Timeout:
                # 入队事务已可见，API 的提交锁可能还在释放；保持心跳等待，避免制造失败重试。
                time.sleep(.05)
        log.info("worker 执行 %s 任务 %s（attempt %s）", kind, task.ledger_id, task.attempt_id[:8])
        runner.run(task.ledger_id, restored, lock)

    return handler
