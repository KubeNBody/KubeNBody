# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The merge-side scripts: begin_merge (lease, fence, simulation time and the
epoch's a_max values in one round trip), publish_epoch (cleanup, enqueue, end
of publication and lease release in one round trip), the task's pipelined
reads, and the commit sent without redis-py's SCRIPT EXISTS pre-check.

Against a real redis-server (conftest's `svc`); skipped without one.
"""
from __future__ import annotations

import pytest

from helpers import bump_generation, commit, requires_redis, set_meta, store_accelerations
from v2.redis_service import (
    _BEGIN_MERGE,
    PENDING_MERGES_KEY,
    STALE_EPOCH,
    STALE_GENERATION,
    TASKS_STREAM,
    rt_count,
    rt_reset,
)


redis = pytest.importorskip("redis")

pytestmark = requires_redis

# the next epoch's tasks of a 3-body, 3-package job, as publish_epoch writes them
TEMPLATE = ["total_packages", "3"]
TASKS = [{"job_id": "1", "epoch": "2", "package_num": str(p), "start_idx": str(p - 1),
          "end_idx": str(p), "total_packages": "3", "time_step": "0.02", "generation": "0"}
         for p in (1, 2, 3)]


def _register_epoch(svc, epoch=1, gen=0, accels=False, amax=False):
    if svc.get_bodies(1) is None:
        svc.store_bodies(1, bytes(3 * 64))
    for p in (1, 2, 3):
        svc.register_partial(1, gen, epoch, p, 3, bytes([p]) * 64, package_size=1,
                             a_max=float(p) if amax else None,
                             accelerations=bytes([p]) * 24 if accels else None)


def _publish(svc, token):
    return svc.publish_epoch(1, 0, 1, token, 3, 3, 1, TEMPLATE, 0.02, "0.02")


# --------------------------------------------------------------------------- #
# begin_merge
# --------------------------------------------------------------------------- #

def test_begin_merge_takes_the_lease_and_reads_the_epoch(svc):
    set_meta(svc, 1, sim_time="0.5")
    _register_epoch(svc, amax=True)
    start = svc.begin_merge(1, 0, 1, "w1", lease_ms=10_000, want_amax=True)
    assert start is not None
    assert svc.merge_lease_owner(1, 0, 1) == start.token
    assert start.fence.generation == "0" and start.fence.committed == 0
    assert start.fence.status == "IN_PROGRESS" and start.sim_time == "0.5"
    assert sorted(start.amax) == [1.0, 2.0, 3.0]


def test_begin_merge_is_one_round_trip_and_survives_a_lost_script_cache(svc):
    """The a_max values are read beside the script in one MULTI/EXEC; a
    primary without the script (a promoted replica) answers NOSCRIPT, and the
    transaction is sent again."""
    set_meta(svc, 1, sim_time="0.5")
    _register_epoch(svc, amax=True)
    svc.client.script_flush()
    start = svc.begin_merge(1, 0, 1, "w1", lease_ms=10_000, want_amax=True)
    assert start is not None and sorted(start.amax) == [1.0, 2.0, 3.0]
    svc.release_merge_lease(1, 0, 1, start.token)
    rt_reset()
    again = svc.begin_merge(1, 0, 1, "w2", lease_ms=10_000, want_amax=True)
    assert again is not None and again.amax == start.amax
    assert rt_count() == 1


def test_begin_merge_leaves_a_held_lease_alone(svc):
    set_meta(svc, 1)
    _register_epoch(svc)
    first = svc.begin_merge(1, 0, 1, "w1", lease_ms=10_000)
    assert svc.begin_merge(1, 0, 1, "w2", lease_ms=10_000) is None
    assert svc.merge_lease_owner(1, 0, 1) == first.token


def test_a_replayed_begin_merge_keeps_its_own_lease(svc):
    """A lost reply retried with the same token finds its own lease."""
    set_meta(svc, 1)
    _register_epoch(svc)
    keys = [svc._lease_key(1, 0, 1), "v2:job:1:meta"]
    args = ["w1:tok", "10000"]
    first = svc._run_script("begin_merge", _BEGIN_MERGE, keys, args)
    again = svc._run_script("begin_merge", _BEGIN_MERGE, keys, args)
    assert int(first[0]) == 1 and int(again[0]) == 1 and first == again
    other = svc._run_script("begin_merge", _BEGIN_MERGE, keys, ["w2:tok"] + args[1:])
    assert list(other) == [0]


def test_begin_merge_reads_no_amax_for_a_committed_epoch_or_a_stale_generation(svc):
    set_meta(svc, 1)
    _register_epoch(svc, amax=True)
    assert svc.commit_staged(1, 0, 1, 3, 0.01)[0] == 1
    start = svc.begin_merge(1, 0, 1, "w1", lease_ms=10_000, want_amax=True)
    assert start.fence.committed == 1 and start.amax is None
    bump_generation(svc, 1)
    stale = svc.begin_merge(1, 0, 2, "w1", lease_ms=10_000, want_amax=True)
    assert not stale.fence.is_current(0) and stale.amax is None


# --------------------------------------------------------------------------- #
# publish_epoch
# --------------------------------------------------------------------------- #

def _committed_epoch_1(svc, **meta):
    set_meta(svc, 1, **meta)
    _register_epoch(svc, accels=True, amax=True)
    start = svc.begin_merge(1, 0, 1, "w1", lease_ms=10_000)
    assert svc.commit_staged(1, 0, 1, 3, 0.01)[0] == 1
    assert svc.client.zscore(PENDING_MERGES_KEY, "1:0:1") is not None
    return start.token


def _epoch_keys(svc, epoch=1, gen=0):
    return [svc._epoch_key(1, epoch, s, gen) for s in ("counted", "amax", "astaging", "staged")]


def _stream(svc):
    return [{k.decode(): v.decode() for k, v in f.items()} for _, f in svc.client.xrange(TASKS_STREAM)]


def test_publish_epoch_cleans_up_enqueues_finishes_and_releases(svc):
    token = _committed_epoch_1(svc)
    cleanup, enqueued, published = _publish(svc, token)
    assert published and enqueued == 3 and cleanup > 0
    assert not any(svc.client.exists(k) for k in _epoch_keys(svc))
    assert _stream(svc) == TASKS
    meta = svc.get_job_meta(1)
    assert meta["current_epoch"] == "2" and float(meta["epoch_dt"]) == 0.02
    assert svc.get_publish_marker(1, 0, 1) is None
    assert svc.client.zscore(PENDING_MERGES_KEY, "1:0:1") is None
    assert svc.merge_lease_owner(1, 0, 1) is None


def test_a_replayed_publish_changes_nothing_more(svc):
    token = _committed_epoch_1(svc)
    _publish(svc, token)
    cleanup, enqueued, published = _publish(svc, token)
    assert published and enqueued == 0 and cleanup == 0
    assert len(_stream(svc)) == 3


def test_publish_releases_only_its_own_lease(svc):
    token = _committed_epoch_1(svc)
    svc.client.set(svc._lease_key(1, 0, 1), "w2:other")      # ours lapsed, w2 took it
    assert _publish(svc, token)[2]
    assert svc.merge_lease_owner(1, 0, 1) == "w2:other"


def test_a_lost_commit_leaves_the_epoch_untouched(svc):
    """A failover lost the commit: committed_epoch is behind the epoch. Nothing
    may change, so a takeover can commit the epoch again."""
    token = _committed_epoch_1(svc)
    svc.update_job_meta(1, "committed_epoch", 0)              # the new primary never saw it
    cleanup, enqueued, published = _publish(svc, token)
    assert (cleanup, enqueued, published) == (STALE_EPOCH, STALE_EPOCH, False)
    assert all(svc.client.exists(k) for k in _epoch_keys(svc)[:2])
    assert _stream(svc) == []
    assert svc.get_publish_marker(1, 0, 1) is not None
    assert svc.client.zscore(PENDING_MERGES_KEY, "1:0:1") is not None
    assert svc.merge_lease_owner(1, 0, 1) == token


def test_a_stale_generation_is_not_enqueued_but_its_publication_ends(svc):
    token = _committed_epoch_1(svc)
    bump_generation(svc, 1)
    cleanup, enqueued, published = _publish(svc, token)
    assert (cleanup, enqueued, published) == (STALE_GENERATION, STALE_GENERATION, True)
    assert _stream(svc) == []
    assert svc.get_publish_marker(1, 0, 1) is None


@pytest.mark.parametrize("status, code", [("PAUSED", -4), ("CANCELLED", -3)])
def test_a_paused_or_cancelled_job_is_cleaned_up_and_finished_without_an_enqueue(svc, status, code):
    token = _committed_epoch_1(svc)
    svc.update_job_meta(1, "status", status)
    cleanup, enqueued, published = _publish(svc, token)
    assert enqueued == code and published and cleanup > 0
    assert _stream(svc) == []
    assert svc.client.zscore(PENDING_MERGES_KEY, "1:0:1") is None
    assert svc.merge_lease_owner(1, 0, 1) is None


def test_publish_matches_the_separate_steps(svc):
    """The script leaves the same keys and stream as cleanup_epoch_data +
    enqueue_epoch_tasks + finish_publication + release_merge_lease."""
    def snapshot():
        keys = sorted(k for k in svc.client.keys("*") if not k.startswith(b"v2:tasks"))
        return keys, svc.get_job_meta(1), _stream(svc)

    token = _committed_epoch_1(svc)
    _publish(svc, token)
    fused = snapshot()
    svc.client.flushdb()
    token = _committed_epoch_1(svc)
    svc.cleanup_epoch_data(1, 1, 0)
    svc.enqueue_epoch_tasks(1, 0, 2, TASKS, 0.02)
    svc.finish_publication(1, 0, 1)
    svc.release_merge_lease(1, 0, 1, token)
    assert snapshot() == fused


# --------------------------------------------------------------------------- #
# The task's reads and the commit pipeline
# --------------------------------------------------------------------------- #

def test_task_inputs_are_one_round_trip(svc):
    set_meta(svc, 1)
    svc.store_bodies(1, b"bodies")
    store_accelerations(svc, 1, b"a0")
    rt_reset()
    fence, bodies, accels = svc.get_task_inputs(1, want_accels=True)
    assert rt_count() == 1
    assert fence == svc.get_fence_state(1) and bodies == b"bodies" and accels == b"a0"
    assert svc.get_task_inputs(1)[2] is None


def test_the_sentinel_commit_pipeline_skips_the_script_check_and_survives_noscript(svc):
    """The commit + WAIT pipeline sends EVALSHA as a plain command (redis-py's
    script call would send SCRIPT EXISTS first, a round trip per commit). A
    primary without the script (a promoted replica) answers NOSCRIPT, after
    which it is loaded and the pipeline sent again."""
    set_meta(svc, 1)
    svc._use_sentinel = True
    svc.client.script_flush()
    assert commit(svc, 1, 0, 1, b"1" * 64, 0.01) == 1               # NOSCRIPT, load, again
    assert svc.get_job_meta(1)["committed_epoch"] == "1"
    svc.register_partial(1, 0, 2, 1, 1, b"2" * 64, package_size=1)
    rt_reset()
    assert svc.commit_staged(1, 0, 2, 1, 0.01)[0] == 1
    assert rt_count() == 1                                          # one pipeline
    assert svc.get_bodies(1) == b"2" * 64
