# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""V2 Redis coordination correctness.

Exercises RedisService's coordination logic against a tiny in-memory fake client
(no real Redis needed — the service runs ops directly in non-sentinel mode):

  * cleanup_job must not delete OTHER jobs' keys. `v2:*:{id}` /
    `v2:*:{id}:*` globs match any key where {id} appears as a trailing or
    segment number, so cleaning job 2 would wipe job 12's `...:epoch:2:counted`
    and job 5's `...:epoch:2:amax`, hanging those jobs.
  * cleanup_epoch_data must clear one epoch's keys and leave other epochs'
    (the generation-fenced script, against the real redis-server of
    conftest's `svc`).
"""
from __future__ import annotations

import fnmatch
import threading
import types

from helpers import requires_redis, set_meta
from v2.redis_service import V2_PREFIX, RedisService


class FakeRedis:
    """Minimal dict-backed Redis supporting exactly what RedisService uses here."""

    def __init__(self):
        self.store = {}   # str -> bytes/int
        self.hashes = {}  # str -> {field: value}

    # strings / counters
    def setex(self, key, ttl, val):
        self.store[key] = val

    def get(self, key):
        return self.store.get(key)

    def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def set(self, key, val, nx=False, ex=None, px=None):
        if nx and (key in self.store):
            return None
        self.store[key] = val
        return True

    # hashes
    def hset(self, key, field=None, value=None, mapping=None):
        h = self.hashes.setdefault(key, {})
        if mapping:
            h.update(mapping)
        else:
            h[field] = value

    def hsetnx(self, key, field, value):
        h = self.hashes.setdefault(key, {})
        if field in h:
            return 0
        h[field] = value
        return 1

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    # misc
    def expire(self, key, ttl):
        return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]; n += 1
            if k in self.hashes:
                del self.hashes[k]; n += 1
        return n

    def scan(self, cursor, match=None, count=100):
        allkeys = set(self.store) | set(self.hashes)
        matched = [k for k in allkeys if match is None or fnmatch.fnmatchcase(k, match)]
        return 0, matched


def _svc():
    svc = RedisService.__new__(RedisService)   # bypass __init__ (no real connection)
    svc._use_sentinel = False
    svc._lock = threading.Lock()
    svc._reclaim_cursors, svc._scripts = {}, {}
    svc.client = FakeRedis()
    return svc


# --------------------------------------------------------------------------- #
# cleanup_job must not touch other jobs
# --------------------------------------------------------------------------- #

def test_cleanup_job_does_not_delete_other_jobs_keys():
    svc = _svc()
    c = svc.client
    # job 2's own keys
    c.setex(f'{V2_PREFIX}bodies:2', 1, b'x')
    c.setex(f'{V2_PREFIX}accelerations:2', 1, b'x')
    c.hset(svc._epoch_key(2, 1, 'counted', 0), 'package_1', b'1')
    c.setex(svc._epoch_key(2, 1, 'amax', 1), 1, b'1')
    # OTHER jobs' keys that a broad glob (*:2, *:2:*) would catch:
    c.setex(svc._epoch_key(12, 2, 'counted', 0), 1, b'live')   # job 12, epoch 2
    c.setex(svc._epoch_key(5, 2, 'amax', 2), 1, b'live')       # job 5, epoch 2, generation 2
    c.setex(f'{V2_PREFIX}bodies:12', 1, b'live')               # job 12 body state

    svc.cleanup_job(2)

    # job 2 gone
    assert f'{V2_PREFIX}bodies:2' not in c.store
    assert f'{V2_PREFIX}accelerations:2' not in c.store
    assert svc._epoch_key(2, 1, 'counted', 0) not in c.hashes
    assert svc._epoch_key(2, 1, 'amax', 1) not in c.store
    # other jobs untouched
    assert c.store.get(svc._epoch_key(12, 2, 'counted', 0)) == b'live'
    assert c.store.get(svc._epoch_key(5, 2, 'amax', 2)) == b'live'
    assert c.store.get(f'{V2_PREFIX}bodies:12') == b'live'


@requires_redis
def test_fenced_cleanup_epoch_data_deletes_only_a_committed_epoch(svc):
    """With a generation, an epoch's keys go only once that epoch is committed
    in that generation (a commit lost in a failover leaves them for the
    takeover that commits it again). Other epochs and generations are kept."""
    set_meta(svc, 1, committed_epoch=4)
    svc.store_bodies(1, bytes(2 * 64))
    svc.register_partial(1, 0, 5, 1, 2, bytes(64), a_max=1.0, accelerations=bytes(24), package_size=1)
    svc.client.set(svc._epoch_key(1, 5, 'enqueued', 0), b"1")
    epoch5 = [svc._epoch_key(1, 5, sfx, 0)
              for sfx in ('counted', 'amax', 'staging', 'astaging', 'staged', 'enqueued')]
    other = [svc._epoch_key(1, 6, 'counted', 0), svc._epoch_key(1, 5, 'counted', 1)]
    for key in other:
        svc.client.hset(key, 'package_1', b"keep")

    svc.cleanup_epoch_data(1, 5, 0)                 # epoch 5 not committed yet
    assert all(svc.client.exists(k) for k in epoch5)

    svc.update_job_meta(1, 'committed_epoch', 5)
    svc.cleanup_epoch_data(1, 5, 1)                 # a stale generation
    assert all(svc.client.exists(k) for k in epoch5)

    svc.cleanup_epoch_data(1, 5, 0)
    assert not any(svc.client.exists(k) for k in epoch5)
    assert all(svc.client.exists(k) for k in other)


def test_finalization_lock_survives_a_lost_set_reply(monkeypatch):
    """A SET NX whose reply was lost, retried, finds the key it created: the
    lock is ours, not another worker's for the 10-minute TTL."""
    svc = _svc()
    real_set = svc.client.set

    def reply_lost(key, val, nx=False, px=None):
        real_set(key, val, nx=nx, px=px)
        return None
    with monkeypatch.context() as m:
        m.setattr(svc.client, "set", reply_lost)
        assert svc.acquire_finalization_lock(7) is not None
    assert svc.acquire_finalization_lock(7) is None        # held: another caller is refused


def test_failed_client_is_replaced_once_and_its_busy_connections_kept():
    """Threads failing on the same client share one reconnect. The old pool
    closes only its idle connections (closing in-use ones would break other
    threads' commands, and a retried non-idempotent one could apply twice)."""
    svc = _svc()
    disconnects, connects = [], []
    old = types.SimpleNamespace(connection_pool=types.SimpleNamespace(
        disconnect=lambda **kw: disconnects.append(kw)))
    svc.client = old

    def connect(**kw):
        connects.append(1)
        svc.client = types.SimpleNamespace(connection_pool=None)
    svc._connect = connect
    threads = [threading.Thread(target=svc._reconnect, args=(old,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert connects == [1]
    assert disconnects == [{"inuse_connections": False}]
