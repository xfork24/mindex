"""外部副作用回执：先登记 dispatch，再调用；未知结果必须人工核对。

服务未声明幂等提交协议时，不能用 HTTP 重试推断提交是否发生。已保存的 submit
回执可重放并继续 poll；同步结果的二进制正文存不可变对象，不进入账本大字段。
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from shared.db.fencing import ExecutionFence, current_attempt
from shared.storage import dumps

_current: ContextVar[object] = ContextVar("external_calls", default=None)
OPERATIONS = frozenset(
    {"submit", "generate", "speak", "write", "complete", "split", "title", "animate", "call", "lyrics"}
)


class ReviewRequired(RuntimeError):
    pass


@contextmanager
def scope(store, resource, *, task_id=None):
    attempt = current_attempt()
    task_id = task_id or (attempt.task.ledger_id if attempt else None)
    token = _current.set(Journal(store, resource, task_id) if task_id else None)
    try:
        yield
    finally:
        _current.reset(token)


def _signature(value):

    if isinstance(value, Path):
        if value.is_file():
            with value.open("rb") as source:
                return {"file_sha256": hashlib.file_digest(source, "sha256").hexdigest()}
        return {"file": value.name}
    if dataclasses.is_dataclass(value):
        return _signature(dataclasses.asdict(value))
    if hasattr(value, "model_dump"):
        return _signature(value.model_dump())
    if isinstance(value, dict):
        return {str(k): _signature(v) for k, v in value.items() if not callable(v)}
    if isinstance(value, (list, tuple)):
        return [_signature(v) for v in value]
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest()}
    if callable(value):
        return None
    return value


class Journal:
    def __init__(self, store, resource, task_id):
        self.store, self.resource, self.task_id = store, resource, task_id
        self.occurrences = {}

    def encode(self, call_id, value):
        if isinstance(value, bytes):
            digest = hashlib.sha256(value).hexdigest()
            key = f"external/{call_id}/{digest}"
            self.store.media.put_immutable(key, io.BytesIO(value), sha256=digest)
            return {"$bytes": key, "sha256": digest}
        if isinstance(value, tuple):
            return {"$tuple": [self.encode(call_id, v) for v in value]}
        if isinstance(value, list):
            return [self.encode(call_id, v) for v in value]
        if isinstance(value, dict):
            return {k: self.encode(call_id, v) for k, v in value.items()}
        if hasattr(value, "model_dump"):
            return {
                "$model": type(value).__name__,
                "module": type(value).__module__,
                "data": value.model_dump(mode="json"),
            }
        if dataclasses.is_dataclass(value):
            return {
                "$dataclass": type(value).__name__,
                "module": type(value).__module__,
                "data": self.encode(call_id, dataclasses.asdict(value)),
            }
        # 校验可恢复的 JSON，禁止在 dispatch 后把不可序列化结果误记为成功。
        json.dumps(value)
        return value

    def decode(self, value):
        if isinstance(value, list):
            return [self.decode(v) for v in value]
        if isinstance(value, dict):
            if "$bytes" in value:
                with self.store.media.open_read(value["$bytes"]) as source:
                    payload = source.read()
                if hashlib.sha256(payload).hexdigest() != value["sha256"]:
                    raise ValueError("外部调用回执正文摘要不符")
                return payload
            if "$tuple" in value:
                return tuple(self.decode(v) for v in value["$tuple"])
            if "$model" in value or "$dataclass" in value:
                # 具体类型由调用方还原；共享层不导入模型包。
                return {**value, "data": self.decode(value["data"])}
            return {k: self.decode(v) for k, v in value.items()}
        return value

    def call(self, operation, fn, args, kwargs, *, request_id=None):
        semantic = hashlib.sha256(
            dumps([self.resource, operation, _signature([args, kwargs])]).encode()
        ).hexdigest()
        ordinal = self.occurrences.get(semantic, 0)
        self.occurrences[semantic] = ordinal + 1
        resource = hashlib.sha256(dumps([semantic, ordinal]).encode()).hexdigest()
        call_id = hashlib.sha256(dumps([self.task_id, resource]).encode()).hexdigest()
        # 逻辑调用 ID 跨重试稳定；每次实际 dispatch 单独绑定回执写入权。
        # 人工确认失败并重试后，旧连接的迟到成功/错误都不能改写新 dispatch。
        dispatch = dumps({"$dispatch": uuid4().hex})
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM external_calls WHERE id=?", (call_id,)).fetchone()
            if old and old["state"] == "complete":
                return self.decode(json.loads(old["receipt"]))
            unknown = db.execute(
                "SELECT id FROM external_calls WHERE resource=? AND state IN "
                "('dispatching','needs_review') LIMIT 1",
                (resource,),
            ).fetchone()
            if unknown:
                raise ReviewRequired(f"外部调用结果未知，禁止重复提交，请核对回执：{unknown[0]}")
            if old and old["state"] == "rejected":
                db.execute(
                    "UPDATE external_calls SET state='dispatching',receipt=? WHERE id=?", (dispatch, call_id)
                )
            else:
                db.execute(
                    "INSERT INTO external_calls(id,resource,task_id,operation,state,receipt) "
                    "VALUES(?,?,?,?,'dispatching',?)",
                    (call_id, resource, self.task_id, operation, dispatch),
                )
        try:
            result = fn(*args, **kwargs)
            receipt = dumps(self.encode(call_id, result))
            # 回执独立于发布：即使调用返回时处理租约已失效，也必须保存可核对的结果。
            # 此连接只允许更新已登记调用的回执，不能用作业务资源发布入口。
            with (
                self.store.backend.connect(execution=_receipt_fence())
                if self.store.pg
                else self.store.connect() as db
            ):
                db.execute(
                    "UPDATE external_calls SET "
                    "state='complete',request_id=?,receipt=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND state='dispatching' AND receipt=?",
                    (
                        request_id(result)
                        if request_id is not None
                        else result
                        if operation.endswith(".submit") and isinstance(result, str)
                        else None,
                        receipt,
                        call_id,
                        dispatch,
                    ),
                )
            return result
        except BaseException as exc:
            with (
                self.store.backend.connect(execution=_receipt_fence())
                if self.store.pg
                else self.store.connect() as db
            ):
                db.execute(
                    "UPDATE external_calls SET state='needs_review',error=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND state='dispatching' AND receipt=?",
                    (str(exc)[:2000], call_id, dispatch),
                )
            raise ReviewRequired(f"外部调用可能已提交，须人工核对后继续：{call_id}") from exc


def _receipt_fence():

    return ExecutionFence()


def guard_service(service, name):
    if service is None or isinstance(service, Guard):
        return service
    return Guard(service, name)


class Guard:
    def __init__(self, service, name):
        self.service, self.name = service, name

    def __getattr__(self, method):
        target = getattr(self.service, method)
        if method not in OPERATIONS or not callable(target):
            return target

        @wraps(target)
        def call(*args, **kwargs):
            journal = _current.get()
            if journal is None:
                return target(*args, **kwargs)
            result = journal.call(self.name + "." + method, target, args, kwargs)
            return _restore(result, target)

        return call


def _restore(value, target):
    # 类型解析交给模型/工具层注册的解码器，防止共享层反向依赖。
    if isinstance(value, dict) and ("$model" in value or "$dataclass" in value):
        module = sys.modules.get(value.get("module", target.__module__))
        cls = getattr(module, value.get("$model", value.get("$dataclass")), None)
        if not isinstance(cls, type) or not (issubclass(cls, BaseModel) or dataclasses.is_dataclass(cls)):
            raise ReviewRequired("回执类型无法恢复，请核对外部结果")
        return cls(**value["data"])
    if isinstance(value, list):
        return [_restore(v, target) for v in value]
    if isinstance(value, tuple):
        return tuple(_restore(v, target) for v in value)
    return value


def journaled_factory(factory):
    @wraps(factory)
    def build(*args, **kwargs):
        service = factory(*args, **kwargs)
        return (
            guard_service(service, factory.__name__.removeprefix("make_"))
            if _current.get() is not None
            else service
        )

    return build


def resolve(store, call_id, *, expected_state, outcome, evidence, request_id=None):
    """操作者明确核对提交结果；保留核对依据，禁止无凭据清空 dispatch 记录。"""
    if request_id is not None and (
        not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 512
    ):
        raise ValueError("request_id 必须是 1–512 字符的字符串")
    if not evidence.strip() or len(evidence) > 4000:
        raise ValueError("请填写外部系统核对依据（最多 4000 字）")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM external_calls WHERE id=?", (call_id,)).fetchone()
        if (
            row is None
            or row["state"] != expected_state
            or row["state"] not in {"dispatching", "needs_review"}
        ):
            raise ValueError("调用状态已变化，请刷新后核对")
        if outcome == "request_id" and row["operation"].endswith(".submit") and request_id:
            db.execute(
                "UPDATE external_calls SET state='complete',request_id=?,receipt=?,error=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (request_id, dumps(request_id), evidence, call_id),
            )
        elif outcome in {"not_submitted", "failed"}:
            db.execute(
                "UPDATE external_calls SET state='rejected',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (evidence, call_id),
            )
        else:
            raise ValueError("只能确认未提交或失败，或为异步 submit 补充可核验的 request_id")


def referenced_objects(store):
    for row in store.rows("SELECT receipt FROM external_calls WHERE receipt IS NOT NULL"):
        pending = [json.loads(row["receipt"])]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if "$bytes" in value:
                    key, digest = value["$bytes"], value["sha256"]
                    if not key.startswith("external/") or key.split("/")[-1] != digest or len(digest) != 64:
                        raise ValueError("外部调用回执对象引用无效")
                    yield key
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
    yield from (row["object_key"] for row in store.rows("SELECT object_key FROM uploaded_assets"))
    yield from (
        row["object_key"]
        for row in store.rows(
            "SELECT p.object_key FROM upload_parts p "
            "JOIN upload_sessions s ON s.id=p.session_id WHERE s.state='pending'"
        )
    )


def call_once(operation, descriptor, fn, *, request_id=None):
    journal = _current.get()
    if journal is None:
        return fn()
    return journal.call(operation, lambda _: fn(), (descriptor,), {}, request_id=request_id)
