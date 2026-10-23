# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""DistributedWorkerV2: merging, takeover, finalization and the task loop.

Driven through the real worker against a real redis-server (conftest's `svc`)
with the three-package harness job of helpers.py.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import sys
import threading
import time
import types

import numpy as np
import pytest
import redis as redis_mod
from prometheus_client import REGISTRY

import task_timing
import v2.file_service_v2 as FS
import v2.file_service_v2 as fs
import v2.orchestrator_v2 as orchmod
import v2.redis_service as rsmod
import workers.distributed_worker as dw
import workers.distributed_worker_v2 as dw2
from constants import DEFAULT_THETA, SOFTENING_SQ
from helpers import (
    ADAPTIVE,
    LEASE_MS,
    N_EPOCHS,
    PACKAGE_SIZE,
    bump_generation,
    capture_snapshots,
    create_job,
    drive,
    patch_where_used,
    reference_run,
    requires_redis,
    run_tasks,
    use_models,
    v2_worker,
)
from utils.metadata import compute_host, cpu_model
from v2.binary_format import BODY_SIZE
from v2.epoch_tasks import EpochParams, build_epoch_tasks
from v2.redis_service import TASKS_STREAM
from workers.backends.cpu_backend import CPUBackend
from workers.backends.redis_backend import RedisBackend
from workers.distributed_worker import DistributedWorker
from workers.distributed_worker_v2 import DistributedWorkerV2
from workers.fault_state import FROZEN


redis = pytest.importorskip("redis")

pytestmark = requires_redis


def _run_loop_once(w, backend, task, monkeypatch):
    """Feed one (re)delivered task through the worker's main loop."""
    calls = {"n": 0}

    def claim_stale(**k):
        calls["n"] += 1
        if calls["n"] > 1:
            w._shutdown_requested = True
            return None
        return task
    monkeypatch.setattr(backend, "claim_stale_task", claim_stale)
    monkeypatch.setattr(backend, "claim_task", lambda **k: None)
    w._queue_name = "q"
    w._run_loop()


# --------------------------------------------------------------------------- #
# Takeover and publication
# --------------------------------------------------------------------------- #

# A takeover racing a late commit resumes publication instead of failing the job.
def test_takeover_after_concurrent_commit_resumes(svc, no_pg, monkeypatch):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    with monkeypatch.context() as m:
        m.setattr(type(w), "_merge_and_advance", lambda self, t, rs: None)
        run_tasks(w, backend, 3)                   # all registered, merger "stalled"
    w, backend, finals = v2_worker(svc)
    real_begin = rsmod.RedisService.begin_merge

    def racing_begin(self, job_id, generation, epoch, *a, **k):
        self.commit_staged(job_id, 0, epoch, 3, 0.01)     # the stalled merger commits
        self.cleanup_epoch_data(job_id, epoch, 0)
        return real_begin(self, job_id, generation, epoch, *a, **k)
    with monkeypatch.context() as m:
        m.setattr(rsmod.RedisService, "begin_merge", racing_begin)
        time.sleep(0.01)
        w._try_takeover()
    assert svc.get_job_meta(1)["status"] == "IN_PROGRESS"
    assert svc.client.xlen(TASKS_STREAM) == 6       # epoch 2 enqueued by the resumed publication
    assert svc.get_pending_merges() == []


# An expired publish marker still leads to the next epoch being enqueued.
def test_expired_publish_marker_still_publishes(svc, no_pg):
    create_job(svc)
    w, backend, finals = v2_worker(svc)

    class Crash(BaseException):
        pass

    def die(*a, **k):
        raise Crash()
    real = w._maybe_ship_accelerations
    w._maybe_ship_accelerations = die
    for _ in range(3):
        t = backend.claim_task("w-test", "q", block_ms=10)
        try:
            w._process_task(t)
        except Crash:
            pass
    w._maybe_ship_accelerations = real
    svc.client.delete(svc._epoch_key(1, 1, "publish", 0))   # expired
    svc.client.delete(svc._lease_key(1, 0, 1))
    w._try_takeover()
    assert svc.client.xlen(TASKS_STREAM) == 6
    assert svc.get_pending_merges() == []


# A committed epoch whose state is gone is reported and dropped.
def test_committed_epoch_without_state_is_reported_and_dropped(svc, no_pg, caplog):
    class Crash(BaseException):
        pass
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    real = w._maybe_ship_accelerations

    def die(*a, **k):
        raise Crash()
    w._maybe_ship_accelerations = die
    for _ in range(3):
        t = backend.claim_task("w-test", "q", block_ms=10)
        try:
            w._process_task(t)
        except Crash:
            pass                                        # committed epoch 1, published nothing
    w._maybe_ship_accelerations = real
    svc.client.delete("v2:bodies:1", svc._lease_key(1, 0, 1))
    with caplog.at_level(logging.WARNING):
        w._try_takeover()
    assert svc.get_pending_merges() == []
    assert any(r.levelno >= logging.ERROR and "state is missing" in r.getMessage()
               for r in caplog.records)


# A lagging publisher cannot attach a(0) of an older state to a newer one.
def test_late_ship_from_lost_lease_publisher_is_fenced(svc, no_pg):
    reference = reference_run(svc, "gvv_accel")
    create_job(svc, method="gvv_accel")
    w, backend, finals = v2_worker(svc)
    orig = w._maybe_ship_accelerations
    saved = {}

    def hook(task, merged, rs, **kw):
        if task.epoch_num == 2:
            saved["args"] = (task, merged)
        if task.epoch_num == 3:
            orig(*saved["args"], rs)                # lagging epoch-2 publisher ships now
        return orig(task, merged, rs, **kw)
    w._maybe_ship_accelerations = hook
    drive(w, backend, svc)
    assert finals["state"] == reference


def test_missing_package_size_does_not_guess(svc, no_pg, monkeypatch):
    """N = 21 in packages of 8 (8/8/5): n_bodies // P = 7 would drop body 21.
    The next epoch is sliced with the size the task carries. A task without
    one, whose job meta lost it too, enqueues nothing."""
    create_job(svc, n_bodies=21, package_size=8)            # 8 / 8 / 5
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 2)
    svc.client.hdel("v2:job:1:meta", "package_size")
    before = svc.client.xlen(TASKS_STREAM)
    run_tasks(w, backend, 1)                         # commits epoch 1, enqueues epoch 2
    assert len(svc.get_bodies(1)) // BODY_SIZE == 21
    new = svc.client.xrange(TASKS_STREAM)[before:]
    assert [(int(f[b"start_idx"]), int(f[b"end_idx"])) for _, f in new] == [(0, 8), (8, 16), (16, 21)]

    svc.client.flushdb()
    create_job(svc, n_bodies=21, package_size=8)
    w, backend, finals = v2_worker(svc)
    real_claim = backend.claim_task

    def claim_without_size(*a, **k):
        task = real_claim(*a, **k)
        if task is not None:
            task.package_size = 0
        return task
    monkeypatch.setattr(backend, "claim_task", claim_without_size)
    run_tasks(w, backend, 2)
    svc.client.hdel("v2:job:1:meta", "package_size")
    before = svc.client.xlen(TASKS_STREAM)
    run_tasks(w, backend, 1)                         # commits epoch 1, must not enqueue
    assert len(svc.get_bodies(1)) // BODY_SIZE == 21
    assert svc.client.xlen(TASKS_STREAM) == before


# A merging worker frozen by NBODY_FAULT_INJECT "...@freeze=S" comes back as a
# harmless zombie.
@pytest.mark.parametrize("point", ["before_commit", "after_commit", "before_publish", "before_enqueue"])
def test_frozen_merging_worker_resumes_as_a_harmless_zombie(svc, no_pg, monkeypatch, reference_state, point):
    """The merging worker of epoch 2 freezes (lease renewer and task heartbeat
    paused) for three lease lengths. Another worker takes the epoch over and
    finishes the job. The frozen one then resumes and must change nothing:
    final state bitwise equal to an uninterrupted run, no extra tasks."""

    freeze_s = 3 * LEASE_MS / 1000
    monkeypatch.setenv("NBODY_FAULT_INJECT", f"{point}@2@1@freeze={freeze_s}")
    create_job(svc)
    a, backend_a, _ = v2_worker(svc)
    b, backend_b, finals = v2_worker(svc)
    b.worker_name = "w-b"
    done = {}

    def run_a():   # worker A processes tasks until its freeze has happened
        while "frozen" not in done:
            task = backend_a.claim_task("w-a", "q", block_ms=10)
            if task is None:
                continue
            a._process_task(task)
            if svc.client.exists(f"v2:fault:1:{point}:2"):
                done["frozen"] = True
            else:
                backend_a.ack_task(task)
        done["a_finished"] = True

    t = threading.Thread(target=run_a, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not FROZEN.is_set() and time.time() < deadline:
        time.sleep(0.01)
    assert FROZEN.is_set(), f"worker A never froze at {point}"
    drive(b, backend_b, svc)                  # B takes epoch 2 over and completes the job
    t.join(freeze_s + 10)
    assert done.get("a_finished"), "the zombie did not return"
    assert finals["state"] == reference_state
    meta = svc.get_job_meta(1)
    assert meta["committed_epoch"] == str(N_EPOCHS) and meta["status"] == "COMPLETED"
    assert backend_b.claim_task("w-c", "q", block_ms=10) is None   # the zombie enqueued nothing new


# --------------------------------------------------------------------------- #
# Finalization
# --------------------------------------------------------------------------- #

# Finalize must not turn a CANCELLED job into COMPLETED.
def test_finalize_keeps_cancelled(svc, no_pg, monkeypatch):
    patch_where_used(monkeypatch, fs, "upload_final_results_csv", lambda *a, **k: "key")
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    t = backend.claim_task("w-test", "q", block_ms=10)
    svc.update_job_meta(1, "status", "CANCELLED")
    assert DistributedWorkerV2._finalize_job(w, t, svc.get_bodies(1), svc)
    assert svc.get_job_meta(1)["status"] == "CANCELLED"


# Finalization is fenced: a recovery or cancel in between wins.
def test_finalize_refuses_stale_generation(svc, no_pg, monkeypatch):
    patch_where_used(monkeypatch, fs, "upload_final_results_csv", lambda *a, **k: "key")
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    t = backend.claim_task("w-test", "q", block_ms=10)
    svc.update_job_meta(1, "committed_epoch", t.num_epochs)
    t.epoch_num = t.num_epochs
    bump_generation(svc, 1)                            # recovered during the last merge
    assert DistributedWorkerV2._finalize_job(w, t, svc.get_bodies(1), svc)
    assert svc.get_job_meta(1)["status"] == "IN_PROGRESS"
    t.generation = 1
    assert DistributedWorkerV2._finalize_job(w, t, svc.get_bodies(1), svc)
    assert svc.get_job_meta(1)["status"] == "COMPLETED"
    assert svc.get_job_meta(1)["results_file_key"] == "key"


# A finalizer from before a recovery does not upload the final results.
def test_stale_finalizer_does_not_upload(svc, no_pg, monkeypatch):
    uploads = []
    patch_where_used(monkeypatch, FS, "upload_final_results_csv", lambda *a: uploads.append(a) or "k")
    backend = RedisBackend(); backend._redis = svc
    w = DistributedWorkerV2(CPUBackend(), backend, worker_name="w-old")
    svc.set_job_meta(9, dict(generation=1, committed_epoch=4, status="IN_PROGRESS"))
    task = types.SimpleNamespace(job_id=9, generation=0, epoch_num=4)   # issued before the recovery
    assert w._finalize_job(task, b"", svc) is True
    assert uploads == [] and svc.get_job_meta(9)["status"] == "IN_PROGRESS"
    task = types.SimpleNamespace(job_id=9, generation=1, epoch_num=4)   # the current finalizer
    w._finalize_job(task, b"", svc)
    assert len(uploads) == 1 and svc.get_job_meta(9)["status"] == "COMPLETED"


# Lost results are reported.
def test_finalize_with_meta_gone_logs_an_error(svc, no_pg, caplog):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    t = backend.claim_task("w-test", "q", block_ms=10)
    svc.client.delete("v2:job:1:meta")
    with caplog.at_level(logging.WARNING):
        assert DistributedWorkerV2._finalize_job(w, t, svc.get_bodies(1), svc)
    assert any(r.levelno >= logging.ERROR and "dropped" in r.getMessage() for r in caplog.records)


def test_finalize_retry_is_not_timed_as_a_takeover(svc, no_pg, monkeypatch):

    monkeypatch.setattr(task_timing, "TIMING_ENABLED", True)
    patch_where_used(monkeypatch, fs, "upload_final_results_csv", lambda *a, **k: "key")
    job_model = sys.modules["models"].Job
    real_update = job_model.update

    def update(**kw):
        if "completed_at" in kw:
            raise ConnectionError("database down")
        return real_update(**kw)
    monkeypatch.setattr(job_model, "update", staticmethod(update))
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    del w._finalize_job                                 # the real one
    run_tasks(w, backend, 3 * N_EPOCHS)
    assert svc.get_job_meta(1)["status"] == "COMPLETED"
    assert len(svc.get_pending_merges()) == 1           # PostgreSQL record still due
    # a retry neither reads the 64 MB/1e6-body state nor uploads anything again
    reads, uploads = [], []
    monkeypatch.setattr(svc, "get_bodies", lambda job_id: reads.append(job_id))
    patch_where_used(monkeypatch, fs, "upload_final_results_csv", lambda *a, **k: uploads.append(a) or "key")
    w._try_takeover()
    w._try_takeover()
    assert reads == [] and uploads == []
    records = [json.loads(r) for r in svc.client.lrange("v2:timings:1", 0, -1)]
    takeovers = [r for r in records if r.get("takeover")]
    assert [r["path"] for r in takeovers] == ["finalize_retry", "finalize_retry"]
    monkeypatch.setattr(job_model, "update", staticmethod(real_update))   # the database is back
    w._try_takeover()
    assert svc.get_pending_merges() == []


# Non-finite computed state fails the job instead of being registered and
# published as the final result.
def test_non_finite_final_epoch_fails_the_job_instead_of_completing(svc, no_pg):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    real_step = w.compute.compute_package_step

    def poisoned(**kw):
        pos, vel, acc = real_step(**kw)
        if kw["k_ids"][0] == 17:                      # the last package ...
            meta = svc.get_job_meta(1)
            if int(meta["committed_epoch"]) == N_EPOCHS - 1:   # ... on the last epoch
                pos = pos.copy(); pos[0, 0] = np.nan
        return pos, vel, acc

    w.compute.compute_package_step = poisoned
    for _ in range(200):
        task = backend.claim_task("w-test", "q", block_ms=10)
        if task is None:
            break
        if w._process_task(task):
            backend.ack_task(task)
    meta = svc.get_job_meta(1)
    assert meta["status"] == "FAILED"
    assert "state" not in finals                      # nothing was finalized
    assert int(meta["committed_epoch"]) == N_EPOCHS - 1


# The final epoch of a v2 job is snapshotted like any other (not only written
# as the results CSV), so its animation and comparison metrics reach the end.
@pytest.mark.parametrize("interval, expected", [(2, [2, 4]), (3, [3, 4])])
def test_v2_snapshots_include_the_final_epoch(svc, no_pg, monkeypatch, interval, expected):
    store = capture_snapshots(monkeypatch)
    create_job(svc, method="velocity_verlet", snapshot_interval=interval)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    assert sorted(store) == expected and expected[-1] == N_EPOCHS
    assert store[N_EPOCHS][0] == finals["state"]


# Under an S3 outage the bounded upload queue may drop interval snapshots, but
# never the final one (the job completes only after it lands).
def test_only_interval_snapshots_are_droppable(svc, no_pg, monkeypatch):
    flags = {}
    patch_where_used(monkeypatch, fs, "upload_snapshot_async",
                        lambda job_id, epoch, data, droppable=False, **k: flags.__setitem__(epoch, droppable))
    create_job(svc, method="velocity_verlet", snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    assert flags == {e: e != N_EPOCHS for e in range(1, N_EPOCHS + 1)}


# snapshot_interval=0 (the API default, "final only") still writes the final
# snapshot, and it is not droppable.
def test_v2_interval_zero_snapshots_only_the_final_epoch(svc, no_pg, monkeypatch):
    flags = {}
    patch_where_used(monkeypatch, fs, "upload_snapshot_async",
                        lambda job_id, epoch, data, droppable=False, **k: flags.__setitem__(epoch, droppable))
    create_job(svc, method="velocity_verlet", snapshot_interval=0)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    assert flags == {N_EPOCHS: False}


# A final snapshot slower than the wait is reported as still uploading (the
# upload goes on), not as failed. The job completes.
def test_slow_final_snapshot_is_not_logged_as_failed(svc, no_pg, monkeypatch, caplog):
    class Slow:
        def result(self, timeout=None):
            raise concurrent.futures.TimeoutError()
    patch_where_used(monkeypatch, fs, "upload_snapshot_async", lambda *a, **k: Slow())
    create_job(svc, snapshot_interval=0)
    w, backend, finals = v2_worker(svc)
    with caplog.at_level(logging.WARNING):
        drive(w, backend, svc)
    messages = [r.getMessage() for r in caplog.records]
    assert any("upload continues in the background" in m for m in messages)
    assert not any("Failed to upload snapshot" in m for m in messages)
    assert svc.get_job_meta(1)["status"] == "COMPLETED"


# --------------------------------------------------------------------------- #
# The task loop: delivery cap, errors, heartbeats
# --------------------------------------------------------------------------- #

# A task delivered past the cap and its one extra attempt fails its job
# instead of looping (Redis counts every delivery: a worker the task kills too).
def test_delivery_cap_fails_the_job(svc, no_pg, monkeypatch):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    task = backend.claim_task("w-test", "q", block_ms=10)
    task.raw_data["__deliveries__"] = str(dw.MAX_TASK_DELIVERIES + 2)
    calls = {"n": 0}

    def claim_stale(**k):
        calls["n"] += 1
        if calls["n"] > 1:
            w._shutdown_requested = True
            return None
        return task
    monkeypatch.setattr(backend, "claim_stale_task", claim_stale)
    monkeypatch.setattr(backend, "claim_task", lambda **k: None)
    w._queue_name = "q"
    w._run_loop()
    assert svc.get_job_meta(1)["status"] == "FAILED"
    # the failed task was acked (the other two were never delivered)
    assert svc.client.xpending("v2:tasks", "workers")["pending"] == 0


# The first delivery over the cap is still attempted: if it succeeds (the
# fault cleared), the job goes on. If it fails, the job fails.
@pytest.mark.parametrize("fails", [False, True])
def test_the_delivery_over_the_cap_is_attempted_once(svc, no_pg, monkeypatch, fails):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    task = backend.claim_task("w-test", "q", block_ms=10)
    task.raw_data["__deliveries__"] = str(dw.MAX_TASK_DELIVERIES + 1)
    monkeypatch.setattr(w, "_job_is_gone", lambda job_id: False)
    if fails:
        monkeypatch.setattr(w, "_load_accelerations", lambda *a: (_ for _ in ()).throw(ValueError("bad")))
    _run_loop_once(w, backend, task, monkeypatch)
    assert svc.get_job_meta(1)["status"] == ("FAILED" if fails else "IN_PROGRESS")
    # registered or not (`counted` marks the package)
    assert set(svc.client.hkeys("v2:job:1:g0:epoch:1:counted")) == ({b"package_1"} if not fails else set())
    assert svc.client.xpending("v2:tasks", "workers")["pending"] == 0


# A Redis error reading the fence after a refused fail_job keeps the task
# (pending, reclaimed later) instead of leaving it neither acked nor nacked.
def test_fence_error_while_failing_keeps_the_task(svc, no_pg, monkeypatch):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    task = backend.claim_task("w-test", "q", block_ms=10)
    monkeypatch.setattr(svc, "fail_job", lambda *a, **k: -1)

    def blip(*a, **k):
        raise redis.ConnectionError("connection reset")
    monkeypatch.setattr(dw2, "_fence", blip)
    assert w._fail_job(task, "x") is False


# A stale task at the delivery cap is dropped, not allowed to fail the job.
def test_delivery_cap_keeps_cancelled_job(svc, no_pg, monkeypatch):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    task = backend.claim_task("w-test", "q", block_ms=10)
    svc.update_job_meta(1, "status", "CANCELLED")
    task.raw_data["__deliveries__"] = str(dw.MAX_TASK_DELIVERIES + 1)
    _run_loop_once(w, backend, task, monkeypatch)
    assert svc.get_job_meta(1)["status"] == "CANCELLED"


def test_delivery_cap_of_stale_task_keeps_recovered_job(svc, no_pg, monkeypatch):
    store = capture_snapshots(monkeypatch)
    orch = create_job(svc, snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 3)
    stale = backend.claim_task("w-test", "q", block_ms=10)     # gen 0, crash-looping
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: sorted(store))
    orch.recover_job(1)
    stale.raw_data["__deliveries__"] = str(dw.MAX_TASK_DELIVERIES + 1)
    _run_loop_once(w, backend, stale, monkeypatch)
    assert svc.get_job_meta(1)["status"] == "IN_PROGRESS"


# A Redis error while failing a job fails it nowhere and keeps the task.
def test_redis_error_while_failing_keeps_the_task(svc, no_pg, monkeypatch):

    create_job(svc)
    w, backend, finals = v2_worker(svc)
    task = backend.claim_task("w-test", "q", block_ms=10)
    task.raw_data["__deliveries__"] = str(dw.MAX_TASK_DELIVERIES + 2)
    pg_updates = []
    monkeypatch.setattr(sys.modules["models"].Job, "update",
                        staticmethod(lambda **kw: pg_updates.append(kw)))

    def blip(*a, **k):
        raise redis.ConnectionError("connection reset")
    monkeypatch.setattr(svc, "fail_job", blip)
    _run_loop_once(w, backend, task, monkeypatch)
    assert pg_updates == []                             # not FAILED in PostgreSQL
    assert svc.get_job_meta(1)["status"] == "IN_PROGRESS"
    pending = svc.client.xpending_range("v2:tasks", "workers", min="-", max="+", count=10)
    assert task.message_id.encode() in [p["message_id"] for p in pending]


# A database error while checking the job keeps the task instead of dropping it.
def test_database_error_does_not_drop_the_task(monkeypatch):


    class Job:
        job_id = None

        @staticmethod
        def get_or_none(*a, **k):
            raise ConnectionError("database down")
    use_models(monkeypatch, types.SimpleNamespace(Job=Job))
    assert DistributedWorker._job_is_gone(7) is False


def test_worker_loop_heartbeats_the_task_it_processes(svc, no_pg, monkeypatch):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    backend.stale_task_ms = 300
    other = RedisBackend(stale_task_ms=300)
    other._redis = svc
    task = backend.claim_task("w-test", "q", block_ms=10)
    seen = {}

    def slow(t):
        time.sleep(0.7)
        seen["reclaimed"] = other.claim_stale_task("w-other", "q")
        return True
    monkeypatch.setattr(w, "_process_task", slow)
    _run_loop_once(w, backend, task, monkeypatch)
    assert seen["reclaimed"] is None
    pending = svc.client.xpending_range("v2:tasks", "workers", min="-", max="+", count=10)
    assert task.message_id.encode() not in [p["message_id"] for p in pending]   # acked


def test_late_publisher_does_not_overwrite_a_snapshot_sidecar(svc, no_pg, monkeypatch):
    """A merger delayed past a takeover publishes its epoch after a later epoch
    has committed. Its snapshot inputs (time, a(0)) are gone from Redis by then.
    Writing the snapshot anyway replaced the sidecar with one without a(0), so a
    recovery from it would recompute a(0) and leave the bitwise path."""
    store = capture_snapshots(monkeypatch)
    create_job(svc, method="gvv_accel", snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    epoch1 = run_tasks(w, backend, 3)[-1]               # commits and publishes epoch 1
    run_tasks(w, backend, 3)                            # epoch 2 commits
    assert int(svc.get_fence_state(1)["committed_epoch"]) == 2
    good = store[1]
    assert good[2] is not None                          # epoch 1 stored its a(0)

    w._publish_epoch(epoch1, good[0], good[1], svc)     # the delayed publisher
    assert store[1] == good and store.times[1][0] is not None


def test_workers_record_the_cpu_that_computed_the_job(svc, no_pg, monkeypatch):
    """Bitwise comparisons between runs hold for the same CPU model (the kernels
    are compiled with fastmath for the host), so each job records the hosts
    that computed it. The finalizer copies them to PostgreSQL."""
    monkeypatch.setenv("NODE_NAME", "aks-workercpu-1")
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    hosts = svc.get_compute_hosts(1)
    assert hosts == [compute_host()]
    assert hosts[0]["node"] == "aks-workercpu-1" and hosts[0]["cpu_model"] == cpu_model() != ""


def test_idle_workers_trim_the_task_stream(svc, no_pg, monkeypatch):
    """The merge path trims the stream only every 50 epochs, which short jobs
    never reach. An idle worker trims it with its periodic consumer cleanup."""
    w, backend, finals = v2_worker(svc)
    w.takeover_interval_s = w.consumer_cleanup_interval_s = 0.0
    pruned = []
    monkeypatch.setattr(svc, "prune_task_stream", lambda stream: pruned.append(stream))
    w._on_idle()
    assert pruned == [backend._stream]


def test_a_failed_merge_does_not_fail_the_registered_task(svc, no_pg, monkeypatch, ):
    """The task that completes an epoch has registered its result before it
    merges, so a merge error does not fail it (it would stay pending and be
    recomputed after the stale threshold). The epoch is left to a takeover."""
    create_job(svc)
    w, backend, finals = v2_worker(svc)

    def broken_merge(task, rs):
        raise redis_mod.ConnectionError("lost the primary mid-merge")
    monkeypatch.setattr(w, "_merge_and_advance", broken_merge)
    tasks = run_tasks(w, backend, 3)                    # _process_task returned True for all
    assert len(tasks) == 3
    assert svc.get_pending_merges() == [(1, 0, 1)]      # epoch 1 waits for a takeover


# --------------------------------------------------------------------------- #
# Result sizes
# --------------------------------------------------------------------------- #

def test_stream_tasks_carry_the_package_size(svc, no_pg):
    """The registration needs it to place the result in its slice of the
    staging key."""
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    task = backend.claim_task("w-test", "q", block_ms=10)
    assert task.package_size == PACKAGE_SIZE


def test_a_wrong_sized_result_is_refused_and_fails_the_job(svc, no_pg, monkeypatch):
    """A result whose length does not fit its package is refused by the
    registration (nothing written), and the registering worker fails the job:
    the error would recur on every retry."""
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    real_register = rsmod.RedisService.register_partial

    def one_body_short(self, *a, **k):
        if k.get("package_num") == 1 and k.get("epoch") == 1:
            k["data"] = k["data"][:-BODY_SIZE]
        return real_register(self, *a, **k)
    monkeypatch.setattr(rsmod.RedisService, "register_partial", one_body_short)
    failed = []
    monkeypatch.setattr(type(w), "_fail_job", lambda self, task, reason: failed.append(reason) or True)
    run_tasks(w, backend, 3)
    assert svc.client.hget("v2:job:1:g0:epoch:1:staged", "s") == b"2"   # the short one not staged
    assert len(failed) == 1 and "does not fit" in failed[0]
    assert svc.get_pending_merges() == []
    assert svc.client.hlen("v2:job:1:g0:epoch:1:counted") == 2         # never counted


# --------------------------------------------------------------------------- #
# Harness, job-meta parsing and metrics
# --------------------------------------------------------------------------- #

# The harness drives the real finalization: fenced script, results key, lock released.
def test_drive_completes_through_the_real_finalize(svc, no_pg):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    meta = svc.get_job_meta(1)
    assert meta["status"] == "COMPLETED" and meta["committed_epoch"] == str(N_EPOCHS)
    assert meta["results_file_key"] == "v2/job_id/1/final_results.csv"
    assert finals["state"] == svc.get_bodies(1)
    assert not svc.client.exists("v2:job:1:finalize_lock")


# Every reader of the job meta parses it the same way, with creation's defaults.
def test_epoch_params_from_meta_defaults_follow_job_creation():
    meta = {"package_size": "8", "total_packages": "3", "num_epochs": "4", "time_step": "0.01"}
    p = EpochParams.from_meta(meta)
    assert (p.snapshot_interval, p.force_algorithm, p.integration_method) == (0, "direct", "symplectic_euler")
    assert (p.barnes_hut_theta, p.softening_sq) == (DEFAULT_THETA, SOFTENING_SQ)
    assert (p.adaptive_enabled, p.adaptive_eta, p.adaptive_dt_min) == (False, 0.02, 1e-10)
    assert p.adaptive_dt_max == 0.01            # creation stores dt_max <= 0 as time_step
    assert not p.gpu_enabled


def test_created_meta_round_trips_to_the_same_tasks(svc, no_pg):
    """The tasks rebuilt from the stored meta (resume, recovery, takeover)
    equal the ones creation queued."""
    create_job(svc, force_algorithm="barnes_hut", barnes_hut_theta=0.7, **ADAPTIVE)
    created = [fields for _id, fields in svc.client.xrange("v2:tasks")]
    rebuilt = build_epoch_tasks(1, 0, 1, 24, 0.01, EpochParams.from_meta(svc.get_job_meta(1)))
    assert [{k.decode(): v.decode() for k, v in t.items()} for t in created] == rebuilt
    w, backend, _ = v2_worker(svc)
    [task] = run_tasks(w, backend, 1)
    assert EpochParams.from_task(task) == EpochParams.from_meta(svc.get_job_meta(1))
    assert task.adaptive_dt_max == 0.05


# Distributed workers report their packages to Prometheus (worker-performance dashboard).
def test_v2_worker_records_package_metrics(svc, no_pg):
    labels = dict(worker_type="distributed_v2", compute_type="cpu", algorithm="direct")

    def sample(name, **extra):
        return REGISTRY.get_sample_value(name, {**labels, **extra}) or 0.0

    before_ok = sample("nbody_worker_packages_processed_total", status="success")
    before_n = sample("nbody_package_processing_seconds_count")
    before_load = REGISTRY.get_sample_value("nbody_package_download_seconds_count",
                                            {"worker_type": "distributed_v2", "compute_type": "cpu"}) or 0.0
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    assert sample("nbody_worker_packages_processed_total", status="success") - before_ok == 3 * N_EPOCHS
    assert sample("nbody_package_processing_seconds_count") - before_n == 3 * N_EPOCHS
    assert REGISTRY.get_sample_value("nbody_package_download_seconds_count",
                                     {"worker_type": "distributed_v2", "compute_type": "cpu"}) - before_load \
        == 3 * N_EPOCHS
    assert REGISTRY.get_sample_value("nbody_worker_busy",
                                     {"worker_type": "distributed_v2", "compute_type": "cpu"}) == 0.0


# A skipped (stale) task is not a processed package.
def test_skipped_task_is_not_counted_as_a_package(svc, no_pg):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    task = backend.claim_task("w-test", "q", block_ms=10)
    svc.update_job_meta(1, "status", "CANCELLED")
    key = {"worker_type": "distributed_v2", "compute_type": "cpu", "algorithm": "direct"}
    before = REGISTRY.get_sample_value("nbody_package_processing_seconds_count", key) or 0.0
    assert w._process_task(task)
    assert (REGISTRY.get_sample_value("nbody_package_processing_seconds_count", key) or 0.0) == before
