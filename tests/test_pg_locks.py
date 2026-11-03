"""PostgreSQL 租约锁验收（plans §7.1/§11 阶段 3）：互斥/共享/超时/fencing/多进程。

经 ``MINDEX_TEST_POSTGRES_DSN`` 门控。覆盖：独占互斥与等待超时、共享并存、
TTL 过期回收、旧 token 续期/释放/提交拒绝（暂停过 TTL 的旧持有者不得复活）、
token 单调不复用、LockProvider 兼容语义（acquire/acquire_ordered 与本地文件锁
同形状）、多进程（线程模拟 + 独立 owner）争用。
"""

from __future__ import annotations

import os
import threading
import time

import pytest
from filelock import Timeout

from mindex.shared.db.locks_pg import LEASE_TTL_SECONDS, PgLockProvider

pytestmark = pytest.mark.requires_postgres

DSN = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")


@pytest.fixture
def providers():
    """空库基线 + 两个独立 owner（模拟两台 worker）共享同一 library 的锁提供者。"""
    import psycopg

    from mindex.shared.db.migrate_pg import migrate_pg
    from mindex.shared.db.pg_backend import PgBackend

    if not DSN:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN")
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with psycopg.connect(DSN) as conn:
        migrate_pg(conn)
    backend = PgBackend(DSN, library_id="lib-locks-test")
    one, two = PgLockProvider(backend, "lib-locks-test"), PgLockProvider(backend, "lib-locks-test")
    assert one.owner_id != two.owner_id
    return one, two


def test_exclusive_mutual_exclusion_and_timeout(providers):
    one, two = providers
    first = one.lease("studio", mode="exclusive", ttl=5)
    assert first is not None and first.token >= 1
    assert two.lease("studio", mode="exclusive", ttl=5, wait=0.1) is None
    assert two.lease("studio", mode="shared", ttl=5, wait=0.1) is None  # 独占期间共享也不可入
    assert first.release()
    second = two.lease("studio", mode="exclusive", ttl=5, wait=0.5)
    assert second is not None and second.token > first.token  # token 单调递增不复用
    assert second.release()


def test_shared_coexist_but_block_exclusive(providers):
    one, two = providers
    first = one.lease("library", mode="shared", ttl=10)
    second = two.lease("library", mode="shared", ttl=10)
    assert first and second
    assert one.lease("library", mode="exclusive", ttl=5, wait=0.1) is None  # 共享挡独占
    assert first.release() and second.release()
    got = one.lease("library", mode="exclusive", ttl=5, wait=0.5)
    assert got is not None
    got.release()


def test_ttl_expiry_recovers_and_old_token_cannot_commit(providers):
    one, two = providers
    stale = one.lease("studio", mode="exclusive", ttl=0.3)
    assert stale is not None
    time.sleep(0.4)  # 暂停/失联超过 TTL
    assert not stale.verify()  # 旧 token 提交校验拒绝（plans §7.1 核心不变式）
    assert not stale.renew(10)  # 不能复活失效租约
    assert not stale.release()
    recovered = two.lease("studio", mode="exclusive", ttl=5)  # 回收后他人可获取
    assert recovered is not None and recovered.token > stale.token
    assert recovered.release()


def test_renew_extends_lease(providers):
    one, _ = providers
    got = one.lease("topics", mode="exclusive", ttl=0.6)
    time.sleep(0.3)
    assert got.renew(5) and got.verify()
    time.sleep(0.4)  # 原始 TTL 早已过期，续期后仍有效
    assert got.verify()
    got.release()


def test_epoch_increments_on_exclusive_grants(providers):
    one, _ = providers
    first = one.lease("studio", mode="exclusive", ttl=5)
    with one.connect() as db:
        row = db.execute("SELECT epoch, counter FROM lock_resources WHERE name='studio'").fetchone()
        first_epoch = int(row["epoch"])
    first.release()
    second = one.lease("studio", mode="exclusive", ttl=5)
    with one.connect() as db:
        row = db.execute("SELECT epoch, counter FROM lock_resources WHERE name='studio'").fetchone()
    assert int(row["epoch"]) == first_epoch + 1  # 独占新授予递增，永不归零
    second.release()


def test_acquire_context_matches_file_lock_semantics(providers):
    one, two = providers
    with one.acquire("studio", timeout=0.2):
        with pytest.raises(Timeout):
            with two.acquire("studio", timeout=0.1):
                pass
    with two.acquire("studio", timeout=0.5):  # 释放后他人可获取（CM 自动释放）
        pass
    with one.acquire_ordered(["library", "studio"], timeout=0.2):
        with pytest.raises(Timeout):
            with two.acquire("studio", timeout=0.1):
                pass
    with pytest.raises(ValueError):
        one.acquire_ordered(["studio", "library"])  # 逆序拒绝


def test_threaded_contenders_see_serialized_exclusive(providers):
    one, two = providers
    active = []
    overlaps = []
    stop = threading.Event()

    def contender(provider):
        while not stop.is_set():
            got = provider.lease("topics", mode="exclusive", ttl=30, wait=2)
            if got is None:
                continue
            active.append(threading.current_thread().name)
            time.sleep(0.01)
            if not got.release():
                overlaps.append(threading.current_thread().name)
            if len(active) >= 12:
                stop.set()

    threads = [threading.Thread(target=contender, args=(provider,))
               for provider in (one, one, two, two)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not any(thread.is_alive() for thread in threads)
    assert len(active) >= 12 and overlaps == []  # 任意时刻至多一个有效独占


def test_lease_constants_match_plan():
    from mindex.shared.db.locks_pg import HEARTBEAT_SECONDS, LeaseLost

    assert LEASE_TTL_SECONDS == 90.0  # plans §7.1：初始 TTL 90s、心跳 20s
    assert HEARTBEAT_SECONDS == 20.0
    assert issubclass(LeaseLost, RuntimeError)
