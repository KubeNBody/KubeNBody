# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""RedisBackend: claiming, reclaiming and heartbeating V2 tasks on the
stream (real redis-server)."""
from __future__ import annotations

import threading
import time
import types

import redis

import v2.redis_service as rs
from helpers import add_task, requires_redis
from v2.redis_service import GPU_TASKS_STREAM, TASKS_STREAM
from workers.backends.redis_backend import RedisBackend


pytestmark = requires_redis


# A malformed stream entry is discarded instead of blocking the reclaim.
def test_malformed_task_is_discarded(svc):
    backend = RedisBackend()
    backend._redis = svc
    svc.ensure_consumer_group()
    add_task(svc, {"job_id": "not-a-number", "epoch": "1"})
    assert backend.claim_task("w", "q", block_ms=10) is None
    assert svc.client.xpending("v2:tasks", "workers")["pending"] == 0


# A live but slow worker's task is not reclaimed (nor counted as a redelivery).
def test_heartbeat_keeps_a_slow_task_from_being_reclaimed(svc):
    mine, other = RedisBackend(stale_task_ms=300), RedisBackend(stale_task_ms=300)
    mine._redis = other._redis = svc
    svc.ensure_consumer_group()
    add_task(svc, {"job_id": "1", "epoch": "1", "package_num": "1"})
    task = mine.claim_task("w1", "q", block_ms=10)
    heartbeat = mine.keep_alive(task, "w1")
    try:
        time.sleep(0.7)                                 # > stale threshold
        assert other.claim_stale_task("w2", "q") is None
    finally:
        heartbeat.stop()
    time.sleep(0.4)                                     # w1 "died": now it is stale
    reclaimed = other.claim_stale_task("w2", "q")
    assert reclaimed is not None and reclaimed.raw_data["__deliveries__"] == "2"
    assert not svc.touch_task("w1", task.message_id)    # never stolen back


# A task the worker gave up on is redelivered after its retry delay, not after
# the stale threshold (which would stall its epoch for that long), and
# the delay is not capped by the stale threshold: the task waits parked
# outside the stream, then comes back as a new entry carrying its deliveries.
def test_nacked_task_is_redelivered_after_the_retry_delay(svc):
    mine, other = RedisBackend(stale_task_ms=200), RedisBackend(stale_task_ms=200)
    mine._redis = other._redis = svc
    mine.nack_retry_ms = 600                             # longer than the stale threshold
    svc.ensure_consumer_group()
    add_task(svc, {"job_id": "1", "epoch": "1", "package_num": "1"})
    task = mine.claim_task("w1", "q", block_ms=10)
    mine.nack_task(task, requeue=True)
    assert svc.client.xpending("v2:tasks", "workers")["pending"] == 0     # parked, not pending
    time.sleep(0.3)                                      # past the stale threshold...
    assert other.claim_stale_task("w2", "q") is None     # ...but not due: not reclaimed
    assert other.claim_task("w2", "q", block_ms=10) is None
    time.sleep(0.4)
    assert other.claim_stale_task("w2", "q") is None     # due: back in the stream...
    again = other.claim_task("w2", "q", block_ms=10)     # ...as a new entry
    assert again is not None and again.message_id != task.message_id
    assert (again.job_id, again.epoch_num, again.package_num) == (1, 1, 1)
    assert again.raw_data["__deliveries__"] == "2"      # counted towards the cap
    assert "__prior_deliveries__" not in again.raw_data
    # a worker dying on the retried task: reclaimed at the stale threshold,
    # its earlier deliveries still counted
    time.sleep(0.3)
    third = mine.claim_stale_task("w1", "q")
    assert third is not None and third.message_id == again.message_id
    assert third.raw_data["__deliveries__"] == "3"


def test_a_nack_of_a_task_no_longer_ours_parks_nothing(svc):
    """A worker whose task was reclaimed by another (it stalled past the stale
    threshold) must not re-queue it as well: that would duplicate it."""
    mine, other = RedisBackend(stale_task_ms=100), RedisBackend(stale_task_ms=100)
    mine._redis = other._redis = svc
    svc.ensure_consumer_group()
    add_task(svc, {"job_id": "1", "epoch": "1", "package_num": "1"})
    task = mine.claim_task("w1", "q", block_ms=10)
    time.sleep(0.2)
    assert other.claim_stale_task("w2", "q") is not None
    mine.nack_task(task, requeue=True)
    assert svc.client.zcard(svc.retry_set_key()) == 0
    assert svc.client.xpending("v2:tasks", "workers")["pending"] == 1     # still w2's


def test_the_retry_delay_grows_with_each_delivery():
    """Redis counts every delivery towards the cap, so a fixed 15 s delay
    would spend the whole budget in about 90 s, failing jobs over a failure
    that clears within minutes. The delay grows fourfold, up to 300 s,
    whatever the stale threshold."""
    b = RedisBackend(stale_task_ms=45_000)
    b.nack_retry_ms = 15_000
    delay = lambda d: b.retry_delay_ms(types.SimpleNamespace(  # noqa: E731
        raw_data={} if d is None else {"__deliveries__": str(d)}))
    assert [delay(None), delay(1), delay(2), delay(3), delay(4), delay(6)] == \
        [15_000, 15_000, 60_000, 240_000, 300_000, 300_000]


# The stale-task scan continues from its cursor: XAUTOCLAIM examines only ten
# pending entries per call, so restarting at 0-0 every time never reached a
# stale task behind more than ten live ones.
def test_reclaim_reaches_a_stale_task_behind_many_live_ones(svc):
    w = RedisBackend(stale_task_ms=60_000)
    w._redis = svc
    svc.ensure_consumer_group()
    for i in range(30):
        add_task(svc, {"job_id": "1", "epoch": "1", "package_num": str(i + 1)})
    tasks = [w.claim_task("w1", "q", block_ms=10) for _ in range(30)]
    stale = tasks[25]
    svc.client.xclaim("v2:tasks", "workers", "w1", 0, [stale.message_id], idle=120_000, justid=True)
    found = None
    for _ in range(5):                                   # a few scans, as the loop makes
        found = w.claim_stale_task("w2", "q") or found
    assert found is not None and found.message_id == stale.message_id


# A consumer group lost from Redis (restart without persistence, a flush) is
# recreated: ensure_consumer_group ran only at startup, and XREADGROUP failed
# with NOGROUP forever (XAUTOCLAIM's NOGROUP was swallowed).
def test_a_lost_consumer_group_is_recreated(svc):
    w = RedisBackend()
    w._redis = svc
    svc.ensure_consumer_group()
    svc.client.delete("v2:tasks")                       # stream and group gone
    assert w.claim_stale_task("w1", "q") is None
    assert svc.client.xinfo_groups("v2:tasks")[0]["name"] in (b"workers", "workers")
    svc.client.delete("v2:tasks")
    assert w.claim_task("w1", "q", block_ms=10) is None
    add_task(svc, {"job_id": "1", "epoch": "1", "package_num": "1"})
    assert w.claim_task("w1", "q", block_ms=10).job_id == 1


# claim_task hands out exactly one task: a larger COUNT left the extra messages
# pending for this consumer, unprocessed until they went stale.
def test_claim_task_claims_exactly_one(svc):
    svc.ensure_consumer_group()
    for i in range(3):
        add_task(svc, {"job_id": "1", "epoch": "1", "package_num": str(i + 1)})
    msg_id, task = svc.claim_task("w1", block_ms=10)
    assert task["package_num"] == "1"
    assert svc.client.xpending("v2:tasks", "workers")["pending"] == 1


# A connection error after XAUTOCLAIM moved the task to us must not orphan it:
# it is returned, only without its delivery count.
def test_stale_claim_survives_an_error_reading_the_delivery_count(svc, monkeypatch):
    svc.ensure_consumer_group()
    add_task(svc, {"job_id": "1", "epoch": "1", "package_num": "1"})
    svc.claim_task("w1", block_ms=10)

    def lost(*a, **k):
        raise redis.ConnectionError("connection lost")
    monkeypatch.setattr(svc.client, "xpending_range", lost)
    claimed = svc.claim_stale_task("w2", min_idle_ms=0)
    assert claimed is not None and "__deliveries__" not in claimed[1]


# The monitoring summary counts the GPU stream's pending tasks too.
def test_pending_info_covers_both_task_streams(svc):
    svc.ensure_consumer_group(TASKS_STREAM)
    svc.ensure_consumer_group(GPU_TASKS_STREAM)
    add_task(svc, {"job_id": "1"}, stream=TASKS_STREAM)
    add_task(svc, {"job_id": "2"}, stream=GPU_TASKS_STREAM)
    add_task(svc, {"job_id": "3"}, stream=GPU_TASKS_STREAM)
    svc.claim_task("cpu-w", block_ms=10, stream=TASKS_STREAM)
    svc.claim_task("gpu-w", block_ms=10, stream=GPU_TASKS_STREAM)
    svc.claim_task("gpu-w", block_ms=10, stream=GPU_TASKS_STREAM)
    info = svc.get_pending_info()
    assert info["pending_count"] == 3
    assert info["streams"] == {TASKS_STREAM: 1, GPU_TASKS_STREAM: 2}
    assert sorted((c["name"], c["pending"]) for c in info["consumers"]) == [("cpu-w", 1), ("gpu-w", 2)]


def test_pending_info_without_streams(svc):
    assert svc.get_pending_info()["pending_count"] == 0


# A Sentinel reconnect asks the existing Sentinel client for the master again
# instead of building (and leaking) a new one each time.
def test_sentinel_reconnect_reuses_the_sentinel_client(monkeypatch):
    built, masters = [], []

    class FakeMaster:
        def ping(self):
            return True

        def execute_command(self, *a, **k):
            return None

        def pipeline(self, *a, **k):
            return None

    class FakeSentinel:
        def __init__(self, *a, **k):
            built.append(self)

        def master_for(self, *a, **k):
            masters.append(FakeMaster())
            return masters[-1]

        def discover_master(self, name):
            return ("10.0.0.1", 6379)

    monkeypatch.setattr(rs, "Sentinel", FakeSentinel)
    monkeypatch.setattr(rs, "cfg", types.SimpleNamespace(
        get_sentinel_hosts=lambda: [("s", 26379)], sentinel_master="m", password=None, db=0,
        socket_timeout=1, socket_connect_timeout=1, max_retries=1, retry_delay=0))
    svc = rs.RedisService.__new__(rs.RedisService)
    svc.client, svc.sentinel, svc._lock = None, None, threading.Lock()
    svc._use_sentinel = True
    svc._connect_sentinel()
    first = svc.client
    svc._reconnect(first)
    assert len(built) == 1 and len(masters) == 2 and svc.client is masters[1]
