"""查询总期限与同一数据库快照；超限明确可重试，不转为全库或假空结果。"""

from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar

_DEADLINE = ContextVar("query_deadline", default=None)
_CONNECTION = ContextVar("query_connection", default=None)
_RESOURCES = ContextVar("query_resources", default=None)


class QueryBudgetExceeded(RuntimeError):
    pass


def remaining(default=None):
    deadline = _DEADLINE.get()
    if deadline is None:
        return default
    value = deadline - time.monotonic()
    if value <= 0:
        raise QueryBudgetExceeded("查询预算已耗尽，请缩小范围或稍后重试")
    return value if default is None else min(value, default)


def active():
    return _DEADLINE.get() is not None


def check():
    remaining()


@contextmanager
def deadline(seconds, *, check_exit=True):
    end = time.monotonic() + seconds
    previous = _DEADLINE.get()
    token = _DEADLINE.set(min(end, previous) if previous is not None else end)
    try:
        check()
        yield
        if check_exit:
            check()
    finally:
        _DEADLINE.reset(token)


def connection_for(store):
    current = _CONNECTION.get()
    return current[1] if current is not None and current[0] == store.cache_identity else None


def own(callback):
    resources = _RESOURCES.get()
    if resources is None:
        raise RuntimeError("分页检索需要查询作用域")
    resources.callback(callback)


@contextmanager
def scope(store, seconds):
    with deadline(seconds), ExitStack() as stack:
        token = _RESOURCES.set(stack)
        try:
            if connection_for(store) is not None:
                yield
                return
            db = stack.enter_context(
                store.backend.connect(snapshot=True) if store.pg else store.backend.connect()
            )
            if not store.pg:
                db.execute("BEGIN")
            bound = _CONNECTION.set((store.cache_identity, db))
            try:
                yield
                check()
            finally:
                _CONNECTION.reset(bound)
        finally:
            _RESOURCES.reset(token)


def http_request(method, url, **kwargs):
    """活动查询使用异步总期限，防止慢速分块响应重置 socket timeout 无限延长。"""
    import asyncio

    import httpx

    if _DEADLINE.get() is None:
        return httpx.request(method, url, **kwargs)
    timeout = kwargs.pop("timeout", remaining())
    trust_env = kwargs.pop("trust_env", False)
    verify = kwargs.pop("verify", True)

    async def request():
        try:
            async with (
                asyncio.timeout(remaining()),
                httpx.AsyncClient(timeout=timeout, trust_env=trust_env, verify=verify) as client,
            ):
                response = await client.request(method, url, **kwargs)
                check()
                return response
        except TimeoutError as exc:
            raise QueryBudgetExceeded("远端请求超过查询总期限，请稍后重试") from exc

    return asyncio.run(request())
