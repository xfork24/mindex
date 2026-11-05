"""持久物理资源占用；任务失联不会证明远端模型请求已经停止。"""
from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from mindex.shared.external_calls import ReviewRequired


def required_groups(config, kind, *, cluster=False):
    groups = sorted(name for name, group in config.gpu.groups.items() if kind in group.queues)
    if cluster and not groups and kind not in config.gpu.cpu_queues:
        raise ValueError(f"任务 {kind} 未声明 GPU 物理资源组；请配置 gpu.groups 或显式声明 gpu.cpu_queues")
    return groups


class GpuResources:
    def __init__(self, store):
        self.store = store

    @contextmanager
    def connect(self):
        # 独立控制事务仅允许占用记录，失效 attempt 仍能保留未知状态。
        with self.store.backend.connect(control=True) if self.store.pg else self.store.connect() as db:
            if not self.store.pg:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def acquire(self, config, task):
        groups = required_groups(config, task.kind, cluster=getattr(self.store, "deployment_form", None) == "cluster")
        if not groups:
            return True
        with self.connect() as db:
            for name in groups:
                capacity = config.gpu.groups[name].capacity
                db.execute("INSERT INTO gpu_resource_groups(id,capacity) VALUES(?,?) ON CONFLICT(id) DO NOTHING",
                           (name, capacity))
                bound = db.execute("SELECT capacity FROM gpu_resource_groups WHERE id=?" +
                                   (" FOR UPDATE" if self.store.pg else ""), (name,)).fetchone()
                if bound[0] != capacity:
                    raise ValueError(f"物理资源组 {name} 的容量与共享声明不一致；请统一部署配置")
            self._reconcile(db)
            for name in groups:
                old = db.execute("SELECT state FROM gpu_allocations WHERE group_id=? AND attempt_id=?",
                                 (name, task.attempt_id)).fetchone()
                if old:
                    if old[0] != "active":
                        raise ReviewRequired(f"物理资源组 {name} 的执行占用需核对，禁止重放 attempt")
                    continue
                unknown = db.execute("SELECT id FROM gpu_allocations WHERE group_id=? AND state='unknown' LIMIT 1",
                                     (name,)).fetchone()
                if unknown:
                    raise ReviewRequired(f"物理资源组 {name} 存在未知远端执行，请先核对占用 {unknown[0]}")
                used = db.execute("SELECT count(*) FROM gpu_allocations WHERE group_id=? AND state='active'",
                                  (name,)).fetchone()[0]
                if used >= config.gpu.groups[name].capacity:
                    return False
            # 所有组同时取得，避免多资源死锁或只持有半组。
            for name in groups:
                db.execute("INSERT INTO gpu_allocations(id,group_id,library_id,task_id,attempt_id,state) "
                           "VALUES(?,?,?,?,?,'active') ON CONFLICT(group_id,attempt_id) DO NOTHING",
                           (uuid4().hex, name, self.store.library_id, task.id, task.attempt_id))
        return True

    def _reconcile(self, db):
        if not self.store.pg:
            return
        db.execute("UPDATE gpu_allocations a SET state='unknown',updated_at=CURRENT_TIMESTAMP "
                   "WHERE a.state='active' AND NOT EXISTS (SELECT 1 FROM task_queue q "
                   "WHERE q.library_id=a.library_id AND q.id=a.task_id AND q.attempt_id=a.attempt_id "
                   "AND q.status='running' AND q.expires_at>=clock_timestamp())")

    def finish(self, task, *, certain):
        with self.connect() as db:
            db.execute("UPDATE gpu_allocations SET state=?,updated_at=CURRENT_TIMESTAMP "
                       "WHERE library_id=? AND attempt_id=? AND state IN ('active','unknown')",
                       ("released" if certain else "unknown", self.store.library_id, task.attempt_id))

    def pending(self):
        with self.connect() as db:
            self._reconcile(db)
            return [dict(row) for row in db.execute(
                "SELECT * FROM gpu_allocations WHERE library_id=? AND state IN ('active','unknown') ORDER BY id",
                (self.store.library_id,)).fetchall()]

    def resolve(self, allocation_id, *, evidence):
        if not evidence.strip() or len(evidence) > 4000:
            raise ValueError("请填写远端模型请求已停止的核对依据（1–4000 字）")
        with self.connect() as db:
            self._reconcile(db)
            changed = db.execute("UPDATE gpu_allocations SET state='released',evidence=?,updated_at=CURRENT_TIMESTAMP "
                                 "WHERE id=? AND library_id=? AND state='unknown'",
                                 (evidence, allocation_id, self.store.library_id)).rowcount
            if changed != 1:
                raise ValueError("占用状态已变化或仍在执行，不能释放；请刷新后核对")
