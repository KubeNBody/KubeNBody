# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The task-stream trim keeps every entry a consumer group may still need.

XTRIM MAXLEN ~10000 ignores the consumer group: with more than 10,000 newer
entries (a job of over 10,000 packages per epoch, or a backlog of concurrent
jobs) it would delete tasks not yet delivered, or pending on a dead worker and
waiting to be reclaimed. prune_task_stream trims below the oldest entry any
group still needs (XTRIM MINID).
"""
from __future__ import annotations

import pytest

from helpers import requires_redis


pytestmark = requires_redis

STREAM = "v2:tasks"
GROUP = "workers"


@pytest.fixture
def stream(svc):
    # small radix-tree nodes, so the approximate trim removes entries in a short stream
    old = svc.client.config_get("stream-node-max-entries")["stream-node-max-entries"]
    svc.client.config_set("stream-node-max-entries", 2)
    svc.ensure_consumer_group(STREAM)
    ids = [svc.client.xadd(STREAM, {"n": i}).decode() for i in range(50)]
    yield svc, ids
    svc.client.config_set("stream-node-max-entries", old)


def _left(svc):
    return {m[0].decode() for m in svc.client.xrange(STREAM)}


def _read(svc, consumer, n):
    return [m[0].decode() for m in svc.client.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=n)[0][1]]


def test_entries_not_yet_delivered_are_kept(stream):
    svc, ids = stream
    svc.prune_task_stream(stream=STREAM)          # nothing delivered yet
    assert _left(svc) == set(ids)


def test_pending_and_undelivered_entries_survive_and_acked_ones_go(stream):
    svc, ids = stream
    got = _read(svc, "w-a", 20)
    svc.client.xack(STREAM, GROUP, *got[:15])     # 15 done, 5 pending on w-a (say it died)
    svc.prune_task_stream(stream=STREAM)
    left = _left(svc)
    assert set(ids[15:]) <= left                  # the 5 pending and the 30 undelivered
    assert len(left) < len(ids)                   # and the acknowledged ones were trimmed
    # the dead worker's tasks can still be reclaimed with their data
    _, claimed, deleted = svc.client.xautoclaim(STREAM, GROUP, "w-b", min_idle_time=0, start_id="0-0")
    assert [m[0].decode() for m in claimed] == ids[15:20] and not deleted


def test_a_fully_processed_prefix_is_trimmed_up_to_the_last_delivered_entry(stream):
    svc, ids = stream
    got = _read(svc, "w-a", 30)
    svc.client.xack(STREAM, GROUP, *got)
    svc.prune_task_stream(stream=STREAM)
    left = _left(svc)
    assert set(ids[30:]) <= left and len(left) < 25


def test_the_oldest_needed_entry_across_groups_wins(stream):
    svc, ids = stream
    svc.client.xgroup_create(STREAM, "other", id="0")   # a second group that has read nothing
    got = _read(svc, "w-a", 30)
    svc.client.xack(STREAM, GROUP, *got)
    svc.prune_task_stream(stream=STREAM)
    assert _left(svc) == set(ids)


def test_pending_tasks_behind_a_large_backlog_are_not_lost(stream):
    """The MAXLEN ~10000 failure: over 10,000 newer entries trimmed a dead
    worker's pending tasks (and their data) before they could be reclaimed."""
    svc, ids = stream
    pending = _read(svc, "w-dead", 5)
    pipe = svc.client.pipeline(transaction=False)
    for i in range(10_100):
        pipe.xadd(STREAM, {"n": i})
    pipe.execute()
    _read(svc, "w-a", 10_100)                     # everything else delivered...
    svc.client.xack(STREAM, GROUP, *ids[5:])     # ...and the first 45 acked
    svc.prune_task_stream(stream=STREAM)
    assert set(pending) <= _left(svc)
    _, claimed, deleted = svc.client.xautoclaim(STREAM, GROUP, "w-b", min_idle_time=0, start_id="0-0", count=5)
    assert [m[0].decode() for m in claimed] == pending and not deleted
