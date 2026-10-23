# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Fenced epoch commit and merger takeover.

Runs against a real redis-server started on a free port (conftest's `svc`),
because the coordination logic lives in Lua scripts. Skipped when no
redis-server binary is available.

Covers:
  * the registration fence (duplicates counted once, stale generation / epoch
    and cancelled jobs rejected, the counter and the pending-merge entry
    updated atomically).
  * compare-and-set commit, once-per-generation enqueue, owner-token leases.
  * the fence on a lost meta hash, TTLs, NOSCRIPT / NOREPLICAS and Sentinel
    retries of the Lua calls.
  * end-to-end: a 3-package job driven through DistributedWorkerV2 whose
    merging worker "dies" at each step of the epoch transition finishes with
    a final state bitwise identical to an uninterrupted run, with no operator
    action (takeover by another worker after the lease expires).
  * recovery bumps the generation and fences out pre-crash tasks. Its own
    writes are fenced on its lock and generation.
"""
from __future__ import annotations

import time

import numpy as np
import pytest
from redis.exceptions import ReadOnlyError

import physics
import v2.file_service_v2 as fs
import v2.orchestrator_v2 as orchmod
import v2.redis_service as rsmod
from helpers import (
    ADAPTIVE,
    LEASE_MS,
    N_BODIES,
    N_EPOCHS,
    WorkerCrash,
    bump_generation,
    commit,
    create_job,
    drive,
    initial_bodies,
    patch_where_used,
    register,
    requires_redis,
    run_tasks,
    set_meta,
    store_accelerations,
    v2_worker,
)
from v2.binary_format import binary_to_numpy
from v2.redis_service import (
    DEFAULT_JOB_TTL_SECONDS,
    JOB_MISSING,
    JOB_NOT_RUNNING,
    JOB_PAUSED,
    PENDING_MERGES_KEY,
    RECOVERY_LOCK_LOST,
    STALE_EPOCH,
    STALE_GENERATION,
    TASKS_STREAM,
)
from workers.distributed_worker_v2 import DistributedWorkerV2


redis = pytest.importorskip("redis")

pytestmark = requires_redis


# --------------------------------------------------------------------------- #
# Lua primitives
# --------------------------------------------------------------------------- #

def test_register_counts_each_package_once_and_marks_epoch_pending(svc):
    set_meta(svc, 1)
    svc.store_bodies(1, bytes(3 * 64))
    assert register(svc, 1, 0, 1, 1, 3) == 1
    assert register(svc, 1, 0, 1, 1, 3) == 1                        # redelivery
    assert register(svc, 1, 0, 1, 2, 3) == 2
    assert svc.client.zscore(PENDING_MERGES_KEY, "1:0:1") is None
    assert register(svc, 1, 0, 1, 3, 3, a_max=2.5) == 3
    assert svc.client.zscore(PENDING_MERGES_KEY, "1:0:1") is not None
    assert svc.get_pending_merges() == [(1, 0, 1)]
    assert svc.client.hlen("v2:job:1:g0:epoch:1:counted") == 3
    assert svc.get_epoch_amax_values(1, 1, 0) == [2.5]


def test_register_rejects_stale_generation_epoch_and_cancelled_jobs(svc):
    set_meta(svc, 1, generation=2, committed_epoch=4)
    assert svc.register_partial(1, 1, 5, 1, 2, b"x", package_size=1) == STALE_GENERATION
    assert svc.register_partial(1, 2, 4, 1, 2, b"x", package_size=1) == STALE_EPOCH    # already merged
    assert svc.register_partial(1, 2, 6, 1, 2, b"x", package_size=1) == STALE_EPOCH    # not issued yet
    assert svc.client.keys("v2:job:1:g2:*") == []                       # nothing written
    svc.update_job_meta(1, "status", "CANCELLED")
    assert svc.register_partial(1, 2, 5, 1, 2, b"x", package_size=1) == JOB_NOT_RUNNING


def test_commit_is_compare_and_set(svc):
    set_meta(svc, 1)
    store_accelerations(svc, 1, b"old-a0")
    state1 = b"1" * 64
    assert commit(svc, 1, 0, 1, state1, 0.01) == 1
    assert svc.get_bodies(1) == state1
    assert svc.get_accelerations(1) is None             # belonged to the old state
    assert svc.get_publish_marker(1, 0, 1) == 0.01
    assert svc.commit_staged(1, 0, 1, 1, 0.01)[0] == 0   # second merger
    assert svc.get_bodies(1) == state1
    svc.finish_publication(1, 0, 1)
    assert svc.get_publish_marker(1, 0, 1) is None


def test_enqueue_once_per_generation_and_respects_pause(svc):
    set_meta(svc, 1)
    tasks = [{"job_id": "1", "package_num": str(i)} for i in (1, 2, 3)]
    svc.update_job_meta(1, "committed_epoch", 1)
    assert svc.enqueue_epoch_tasks(1, 0, 2, tasks, 0.01) == 3
    assert svc.enqueue_epoch_tasks(1, 0, 2, tasks, 0.01) == 0     # replayed publish
    assert svc.client.xlen(TASKS_STREAM) == 3
    assert svc.get_job_meta(1)["current_epoch"] == "2"
    assert svc.enqueue_epoch_tasks(1, 0, 3, tasks, 0.01) == STALE_EPOCH   # epoch 2 not committed
    svc.update_job_meta(1, "committed_epoch", 2)
    svc.update_job_meta(1, "status", "PAUSED")
    assert svc.enqueue_epoch_tasks(1, 0, 3, tasks, 0.01) == JOB_PAUSED
    assert svc.enqueue_epoch_tasks(1, 0, 3, tasks, 0.01, allow_paused=True) == 3
    gen = bump_generation(svc, 1)
    assert svc.enqueue_epoch_tasks(1, 0, 3, tasks, 0.01, allow_paused=True) < 0   # old generation
    svc.update_job_meta(1, "committed_epoch", 1)                                 # recovery restores epoch 1
    assert svc.enqueue_epoch_tasks(1, gen, 2, tasks, 0.01, allow_paused=True) == 3  # re-issue after recovery


def test_merge_lease_is_owner_tokened_and_expires(svc):
    set_meta(svc, 1)
    t1 = svc.begin_merge(1, 0, 5, "w1", lease_ms=200).token
    assert svc.begin_merge(1, 0, 5, "w2", lease_ms=200) is None
    assert not svc.release_merge_lease(1, 0, 5, "w2:forged")
    assert svc.renew_merge_lease(1, 0, 5, t1, lease_ms=200)
    time.sleep(0.3)
    t2 = svc.begin_merge(1, 0, 5, "w2", lease_ms=200).token          # w1 died
    assert not svc.renew_merge_lease(1, 0, 5, t1)                    # w1 can't reclaim it
    assert not svc.release_merge_lease(1, 0, 5, t1)
    assert svc.release_merge_lease(1, 0, 5, t2)


def test_fail_job_is_fenced_on_generation(svc):
    set_meta(svc, 1, generation=1)
    assert svc.fail_job(1, "boom", 0) == STALE_GENERATION
    assert svc.get_job_meta(1)["status"] == "IN_PROGRESS"
    assert svc.fail_job(1, "boom", 1) == 1
    assert svc.get_job_meta(1)["status"] == "FAILED"


# A recovery's generation bump needs its lock and a job that is not cancelled.
# Its state writes and key deletions need its generation to be the live one.
def test_recovery_writes_are_fenced(svc):
    set_meta(svc, 1, generation=4, committed_epoch=7, status="FAILED")
    svc.store_bodies(1, b"live")
    token = svc.acquire_recovery_lock(1)
    assert token and svc.acquire_recovery_lock(1) is None          # held
    assert svc.begin_recovery(1, "not-the-token") == RECOVERY_LOCK_LOST
    assert svc.begin_recovery(1, token) == 5
    assert svc.get_job_meta(1)["status"] == "IN_PROGRESS"
    assert svc.begin_recovery(1, token) == 6                        # a later recovery
    assert svc.restore_recovered_state(1, 5, b"old", b"acc", 2, 0.5, 1.5) == STALE_GENERATION
    svc.client.set("v2:job:1:g6:epoch:8:counted", "x")
    svc.client.set("v2:job:1:g5:epoch:3:counted", "x")
    assert svc.delete_other_generation_keys(1, 5) == STALE_GENERATION
    assert svc.client.exists("v2:job:1:g6:epoch:8:counted", "v2:job:1:g5:epoch:3:counted") == 2
    meta = svc.get_job_meta(1)
    assert svc.get_bodies(1) == b"live" and meta["committed_epoch"] == "7"
    assert svc.restore_recovered_state(1, 6, b"new", None, 2, 0.5, None, committed_dt=0.25) == 1
    meta = svc.get_job_meta(1)
    assert svc.get_bodies(1) == b"new" and not svc.client.exists("v2:accelerations:1")
    assert (meta["committed_epoch"], meta["current_epoch"], meta["next_dt"], meta["committed_dt"]) == \
        ("2", "2", "0.5", "0.25") and "sim_time" not in meta
    assert svc.delete_other_generation_keys(1, 6) == 1
    assert svc.client.exists("v2:job:1:g6:epoch:8:counted")
    assert svc.cancel_job(1) == 1
    assert svc.restore_recovered_state(1, 6, b"x", None, 0, 0.5, None) == JOB_NOT_RUNNING
    assert svc.begin_recovery(1, token) == JOB_NOT_RUNNING          # a cancelled job stays so
    assert svc.get_job_meta(1)["generation"] == "6"
    svc.release_recovery_lock(1, "not-the-token")
    assert svc.client.exists("v2:job:1:recover_lock")
    svc.release_recovery_lock(1, token)
    assert not svc.client.exists("v2:job:1:recover_lock")
    assert svc.begin_recovery(2, svc.acquire_recovery_lock(2)) == JOB_MISSING
    assert not svc.client.exists("v2:job:2:meta")                   # never recreated


# Only the epoch after the committed one is enqueued.
def test_enqueue_of_an_old_epoch_is_refused(svc, no_pg):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 6)                         # epochs 1, 2 committed. 3 queued
    tasks = [{"job_id": "1", "package_num": str(i)} for i in (1, 2, 3)]
    assert svc.enqueue_epoch_tasks(1, 0, 2, tasks, 0.123) == STALE_EPOCH
    meta = svc.get_job_meta(1)
    assert meta["current_epoch"] == "3" and meta["epoch_dt"] != "0.123"


def test_v2_stale_commit_cannot_move_the_clock(svc, no_pg):
    create_job(svc, **ADAPTIVE)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 3)
    before = svc.get_job_meta(1)["sim_time"]
    assert svc.commit_staged(1, 7, 2, 3, 0.01, sim_time=99.0)[0] < 0      # wrong generation
    assert svc.get_job_meta(1)["sim_time"] == before


# Epoch keys outlive long gaps between packages.
def test_epoch_keys_use_the_job_ttl(svc):
    set_meta(svc, 1)
    svc.store_bodies(1, bytes(3 * 64))
    register(svc, 1, 0, 1, 1, 3, a_max=1.0)
    for s in ("counted", "amax", "staging"):
        assert svc.client.ttl(svc._epoch_key(1, 1, s, 0)) > DEFAULT_JOB_TTL_SECONDS - 60


# The fence must not fail open when the meta hash is gone.
def test_commits_refresh_the_meta_ttl(svc, no_pg):
    create_job(svc)
    svc.client.expire("v2:job:1:meta", 100)           # close to expiry
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 3)                         # epoch 1 committed
    assert svc.client.ttl("v2:job:1:meta") > 1000


def test_missing_meta_closes_the_fence(svc, no_pg):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 3)
    t = backend.claim_task("w-test", "q", block_ms=10)
    svc.client.delete("v2:job:1:meta")                # expired / evicted / deleted
    assert w._should_skip(t)
    assert svc.register_partial(1, 0, 2, 1, 3, b"x" * 8, package_size=1) == JOB_NOT_RUNNING
    assert svc.commit_staged(1, 0, 2, 3, 0.01)[0] == JOB_NOT_RUNNING
    assert svc.ship_accelerations(1, 0, 1, b"a") == JOB_NOT_RUNNING
    assert svc.enqueue_epoch_tasks(1, 0, 2, [{"a": "1"}], 0.01) == JOB_NOT_RUNNING
    assert svc.fail_job(1, "x", 0) == JOB_NOT_RUNNING
    assert not svc.client.exists("v2:job:1:meta")     # never recreated


# NOSCRIPT reload and NOREPLICAS
def test_noscript_after_flush_reloads(svc):
    set_meta(svc, 1)
    svc.store_bodies(1, bytes(2 * 64))
    assert register(svc, 1, 0, 1, 1, 2) == 1
    svc.client.script_flush()
    assert register(svc, 1, 0, 1, 2, 2) == 2


def test_noreplicas_is_an_error_and_writes_nothing(svc):
    set_meta(svc, 1)
    svc.store_bodies(1, bytes(2 * 64))
    register(svc, 1, 0, 1, 1, 2)
    svc.client.config_set("min-replicas-to-write", 1)
    try:
        with pytest.raises(redis.ResponseError, match="NOREPLICAS"):
            register(svc, 1, 0, 1, 2, 2)
        assert svc.client.hlen("v2:job:1:g0:epoch:1:counted") == 1
        svc.client.config_set("min-replicas-to-write", 0)
        register(svc, 1, 0, 1, 2, 2)
        svc.client.config_set("min-replicas-to-write", 1)
        with pytest.raises(redis.ResponseError, match="NOREPLICAS"):
            svc.commit_staged(1, 0, 1, 2, 0.01)
    finally:
        svc.client.config_set("min-replicas-to-write", 0)
    assert svc.get_bodies(1) == bytes(2 * 64)
    assert svc.get_job_meta(1)["committed_epoch"] == "0"


# A Sentinel commit that meets a demoted primary is retried.
def test_sentinel_commit_retries_a_returned_readonly_error(svc, monkeypatch):
    set_meta(svc, 1)
    svc.store_bodies(1, bytes(64))
    register(svc, 1, 0, 1, 1, 1)
    svc._use_sentinel = True
    monkeypatch.setattr(rsmod.cfg, "failover_base_delay", 0.01, raising=False)
    reconnects = []
    monkeypatch.setattr(svc, "_connect", lambda *a, **k: reconnects.append(1))
    real_pipeline = svc.client.pipeline
    calls = {"n": 0}

    def pipeline(*a, **k):
        pipe = real_pipeline(*a, **k)
        calls["n"] += 1
        if calls["n"] == 1:     # raise_on_error=False returns the error as a value
            pipe.execute = lambda raise_on_error=True: [
                ReadOnlyError("You can't write against a read only replica."), 0]
        return pipe
    monkeypatch.setattr(svc.client, "pipeline", pipeline)
    assert svc.commit_staged(1, 0, 1, 1, 0.01)[0] == 1
    assert calls["n"] == 2 and reconnects
    assert svc.get_job_meta(1)["committed_epoch"] == "1"


# --------------------------------------------------------------------------- #
# End-to-end with the real worker
# --------------------------------------------------------------------------- #

def test_uninterrupted_run_matches_monolithic_steps(svc, no_pg, reference_state):
    """Sanity: with the acceleration-free Euler method the distributed result
    equals the monolithic stepper bitwise (it only evaluates forces at the
    epoch start), so the harness drives the real pipeline correctly."""
    create_job(svc, method="symplectic_euler")
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    p, v, m, ids = binary_to_numpy(finals["state"])
    b = initial_bodies()
    p0 = np.array([[x["x"], x["y"], x["z"]] for x in b])
    v0 = np.array([[x["vx"], x["vy"], x["vz"]] for x in b])
    m0 = np.array([x["m"] for x in b])
    ids0 = np.arange(1, N_BODIES + 1, dtype=np.int64)
    for _ in range(N_EPOCHS):
        p0, v0, _a = physics.step_symplectic_euler(p0, v0, m0, ids0, 0.01, softening_sq=1e-2)
    assert np.array_equal(p, p0) and np.array_equal(v, v0)


# (method, index of the epoch argument, epoch, crash after the call)
CRASH_POINTS = {
    "before_commit": ("commit_staged", 3, 2, False),             # holds the lease, nothing committed
    "after_commit": ("_maybe_ship_accelerations", None, 2, False),  # committed, nothing published
    # the publication script of a non-final epoch (cleanup, enqueue, end of
    # publication and lease release in one step): not run, or all done
    "before_publish": ("publish_epoch", 3, 2, False),
    "after_publish": ("publish_epoch", 3, 2, True),
    # the final epoch keeps separate steps
    "final_cleanup": ("cleanup_epoch_data", 2, N_EPOCHS, False),
    "final_finish": ("finish_publication", 3, N_EPOCHS, False),  # finalized, still pending
}


@pytest.mark.parametrize("point", list(CRASH_POINTS))
def test_merger_death_is_recovered_by_takeover(svc, no_pg, monkeypatch, reference_state, point):
    """The merging worker dies at `point` while merging epoch 2 and leaves its
    lease behind. After the lease expires another worker finishes the epoch and
    the job completes with the same final state as an uninterrupted run."""
    method, idx, on_epoch, after = CRASH_POINTS[point]
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    real_release = rsmod.RedisService.release_merge_lease
    hit = {"done": False}

    def die():
        hit["done"] = True
        # a dead process never releases its lease: it must expire
        monkeypatch.setattr(rsmod.RedisService, "release_merge_lease", lambda *a, **k: False)
        raise WorkerCrash(point)

    if idx is None:
        orig = w._maybe_ship_accelerations

        def hook(task, merged, rs, **kw):
            if not hit["done"] and task.epoch_num == on_epoch:
                die()
            return orig(task, merged, rs, **kw)

        w._maybe_ship_accelerations = hook
    else:
        orig = getattr(rsmod.RedisService, method)

        def hook(*args, **kwargs):
            if not hit["done"] and args[idx] == on_epoch and not after:
                die()
            result = orig(*args, **kwargs)
            if not hit["done"] and args[idx] == on_epoch:
                die()
            return result

        monkeypatch.setattr(rsmod.RedisService, method, hook)

    def restore_release():
        monkeypatch.setattr(rsmod.RedisService, "release_merge_lease", real_release)

    drive(w, backend, svc, before_takeover=restore_release)
    assert hit["done"], "crash point was never reached"
    assert finals["state"] == reference_state
    # A merger that died after enqueueing leaves only its pending entry. The job
    # runs on, and the next takeover scan sees the epoch is past and drops it.
    restore_release()
    time.sleep(LEASE_MS / 1000 + 0.05)
    w._try_takeover()
    assert svc.get_pending_merges() == []
    assert svc.get_job_meta(1)["committed_epoch"] == str(N_EPOCHS)


def test_duplicate_delivery_after_commit_is_dropped(svc, no_pg, reference_state):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    # run epoch 1 to completion, keeping one task message for replay
    first = backend.claim_task("w-test", "q", block_ms=10)
    assert w._process_task(first)
    backend.ack_task(first)
    for _ in range(2):
        t = backend.claim_task("w-test", "q", block_ms=10)
        assert w._process_task(t)
        backend.ack_task(t)
    assert svc.get_job_meta(1)["committed_epoch"] == "1"
    assert w._should_skip(first)                  # redelivered epoch-1 task
    assert svc.register_partial(1, 0, 1, 1, 3, b"late", package_size=1) == STALE_EPOCH
    drive(w, backend, svc)
    assert finals["state"] == reference_state


def test_recovery_fences_out_tasks_from_before_the_crash(svc, no_pg, monkeypatch):
    orch = create_job(svc)
    w, backend, finals = v2_worker(svc)
    # complete epoch 1, then "lose" the cluster mid-epoch 2 and recover from epoch 1
    for _ in range(3):
        t = backend.claim_task("w-test", "q", block_ms=10)
        assert w._process_task(t)
        backend.ack_task(t)
    snapshot = svc.get_bodies(1)
    stale = backend.claim_task("w-test", "q", block_ms=10)       # an epoch-2 task, gen 0
    assert stale.generation == 0 and stale.epoch_num == 2

    monkeypatch.setattr(orchmod, "list_snapshots", lambda job_id: [1])
    monkeypatch.setattr(orchmod, "download_snapshot", lambda job_id, e: snapshot)
    patch_where_used(monkeypatch, fs, "validate_binary_data", lambda data: True)
    patch_where_used(monkeypatch, fs, "download_snapshot_aux", lambda job_id, e: fs.SnapshotAux())   # no S3 here
    assert orch.recover_job(1) == 1
    meta = svc.get_job_meta(1)
    assert meta["generation"] == "1" and meta["committed_epoch"] == "1"

    # the pre-crash task is skipped and cannot register
    assert w._should_skip(stale)
    assert svc.register_partial(1, 0, 2, stale.package_num, 3, b"x", package_size=1) == STALE_GENERATION
    backend.ack_task(stale)
    drive(w, backend, svc)
    assert svc.get_job_meta(1)["status"] == "COMPLETED"


def test_commit_lost_in_a_failover_is_redone_by_a_takeover(svc, no_pg, monkeypatch, reference_state):
    """A Sentinel failover can lose an acknowledged commit that had not reached
    a replica. The publisher then finds the next epoch refused. The cleanup is
    fenced, so the epoch stays pending with its staged state, and a takeover
    commits it again, bitwise as the uninterrupted run."""
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    real_commit = rsmod.RedisService.commit_staged
    lost = {"done": False}

    def commit_then_lose(self, job_id, generation, epoch, *a, **k):
        # the failover loses the rename with the rest of the commit: the
        # staged slices are still there
        keys = self._job_keys(job_id) + [self._epoch_key(job_id, epoch, sfx, generation)
                                         for sfx in ("staging", "astaging", "staged")]
        before = {key: self.client.dump(key) for key in keys}
        result = real_commit(self, job_id, generation, epoch, *a, **k)
        if epoch == 2 and not lost["done"]:
            lost["done"] = True             # the new primary never saw this commit
            for key, blob in before.items():
                if blob is None:
                    self.client.delete(key)
                else:
                    self.client.restore(key, 0, blob, replace=True)
        return result

    monkeypatch.setattr(rsmod.RedisService, "commit_staged", commit_then_lose)
    drive(w, backend, svc)
    assert lost["done"]
    assert finals["state"] == reference_state


def test_a_takeover_records_the_step_the_epoch_took(svc, no_pg):
    """A takeover that publishes epoch E after E+1 was enqueued used epoch_dt,
    by then E+1's step, as E's dt_used in the snapshot sidecar (adaptive runs)."""
    create_job(svc)
    assert commit(svc, 1, 0, 1, svc.get_bodies(1), 0.02, epoch_dt=0.01) == 1
    svc.update_job_meta(1, "epoch_dt", "0.02")            # epoch 2 enqueued with its own step
    task = DistributedWorkerV2._task_from_meta(1, 0, 1, svc.get_job_meta(1))
    assert task.time_step == 0.01


def test_finalization_lock_is_released_only_by_its_owner(svc, no_pg):
    """A finalizer whose lock expired must not delete the lock of the worker
    that took it over."""
    first = svc.acquire_finalization_lock(1, ttl_seconds=60)
    assert first and svc.acquire_finalization_lock(1) is None
    svc.client.delete("v2:job:1:finalize_lock")              # first's lock expires
    second = svc.acquire_finalization_lock(1)
    svc.release_finalization_lock(1, first)                  # the late release
    assert svc.acquire_finalization_lock(1) is None           # second still holds it
    svc.release_finalization_lock(1, second)
    assert svc.acquire_finalization_lock(1)
