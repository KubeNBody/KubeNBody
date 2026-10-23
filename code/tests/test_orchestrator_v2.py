# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""OrchestratorV2: recovery from snapshots (or the input), concurrent recoveries,
pause / resume / cancel.

Driven through the real worker against a real redis-server, with snapshot
uploads kept in memory (helpers.capture_snapshots).
"""
from __future__ import annotations

import time
import types

import pytest
from botocore.exceptions import ClientError

import file_service
import v2.file_service_v2 as fs
import v2.orchestrator_v2 as orchmod
import v2.redis_service as rs
from helpers import (
    ADAPTIVE,
    N_BODIES,
    N_EPOCHS,
    PACKAGE_SIZE,
    capture_snapshots,
    create_job,
    drive,
    initial_bodies,
    patch_where_used,
    reference_run,
    requires_redis,
    run_tasks,
    v2_worker,
)
from utils.file_parsing import convert_to_csv_format
from v2.orchestrator_v2 import RecoveryConflict
from v2.redis_service import PENDING_MERGES_KEY, TASKS_STREAM


pytestmark = requires_redis


def _pg_job(meta, **overrides):
    """PostgreSQL Job row of a job, as recover_job reads it to rebuild the meta."""
    fields = dict(
        num_epochs=int(meta["num_epochs"]), time_step=float(meta["time_step"]),
        num_packages=int(meta["total_packages"]), package_size=int(meta["package_size"]),
        snapshot_interval=int(meta["snapshot_interval"]), input_bucket="b", input_file_key="k",
        force_algorithm="direct", barnes_hut_theta=0.5, softening_sq=float(meta["softening_sq"]),
        integration_method=meta["integration_method"], adaptive_enabled=False,
        adaptive_eta=0.02, adaptive_dt_min=1e-10, adaptive_dt_max=None,
        gpu_enabled=False, partition_strategy="linear", partition_seed=0)
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def _flush_job_redis(svc, job_id=1):
    """Lose the job's Redis state (bodies, accelerations, meta), as a Redis flush would."""
    svc.client.delete(f"v2:bodies:{job_id}", f"v2:accelerations:{job_id}")
    for k in list(svc.client.scan_iter(match=f"v2:job:{job_id}:*")):
        svc.client.delete(k)


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #

# The old generation's in-flight epoch must not leak into the new one.
def test_recovery_starts_every_epoch_afresh(svc, no_pg, monkeypatch):
    reference = reference_run(svc)
    orch = create_job(svc)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 3)                       # epoch 1 committed
    snapshot_e1 = svc.get_bodies(1)
    run_tasks(w, backend, 3)                       # epoch 2 committed
    run_tasks(w, backend, 2)                       # gen 0, epoch 3: packages 1-2 registered

    monkeypatch.setattr(orchmod, "list_snapshots", lambda job_id: [1])
    monkeypatch.setattr(orchmod, "download_snapshot", lambda job_id, e: snapshot_e1)
    patch_where_used(monkeypatch, fs, "validate_binary_data", lambda data: True)
    patch_where_used(monkeypatch, fs, "download_snapshot_aux", lambda job_id, e: fs.SnapshotAux())   # no S3 here
    assert orch.recover_job(1) == 1
    backend.ack_task(backend.claim_task("w-test", "q", block_ms=10))   # stale gen-0 task
    assert not list(svc.client.scan_iter(match="v2:job:1:g0:*"))       # old generation dropped
    run_tasks(w, backend, 3)                       # gen 1, epoch 2

    registered = {}
    orig = svc.register_partial

    def spy(*a, **k):
        r = orig(*a, **k)
        registered[k.get("package_num")] = r
        return r
    svc.register_partial = spy
    tasks = [backend.claim_task("w-test", "q", block_ms=10) for _ in range(3)]
    assert w._process_task(tasks[2])
    assert registered[3] == 1                       # counts only gen-1 packages
    svc.register_partial = orig
    for t in tasks[:2]:
        assert w._process_task(t)
    for t in tasks:
        backend.ack_task(t)
    drive(w, backend, svc)
    assert finals["state"] == reference


# A meta rebuilt from PostgreSQL must not reuse a generation.
def test_rebuilt_meta_does_not_reuse_a_generation(svc, no_pg, monkeypatch):
    store = capture_snapshots(monkeypatch)
    orch = create_job(svc, snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 3)                           # epoch 1 committed + snapshot 1
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: sorted(store))
    assert orch.recover_job(1) == 1                     # first recovery: generation 1
    pg = _pg_job(svc.get_job_meta(1))
    for p in (1, 2, 3):                                 # gen-1 epoch 2 mergeable, merger dies
        n = min(PACKAGE_SIZE, N_BODIES - (p - 1) * PACKAGE_SIZE)
        assert svc.register_partial(1, 1, 2, p, 3, bytes(64 * n), package_size=PACKAGE_SIZE) == p
    assert svc.client.zscore("v2:pending_merges", "1:1:2") is not None
    _flush_job_redis(svc)
    assert orch.recover_job(1, pg_job=pg) == 1          # meta rebuilt from PostgreSQL
    assert int(svc.get_job_meta(1)["generation"]) > 1_000_000_000_000
    w._try_takeover()                                   # the stale entry is dropped
    assert svc.get_job_meta(1)["status"] == "IN_PROGRESS"
    assert svc.client.zscore("v2:pending_merges", "1:1:2") is None
    drive(w, backend, svc)
    assert svc.get_job_meta(1)["status"] == "COMPLETED"


# Recovering a finished job (its latest snapshot is the final epoch) completes
# it again through a takeover, instead of leaving it IN_PROGRESS with nothing
# queued until the watchdog fails it. Also with the meta rebuilt from
# PostgreSQL, keeping the adaptive step that reached the final state.
@pytest.mark.parametrize("rebuild", [False, True])
def test_recovery_from_the_final_snapshot_completes_again(svc, no_pg, monkeypatch, rebuild):
    store = capture_snapshots(monkeypatch)
    orch = create_job(svc, snapshot_interval=1, **ADAPTIVE)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    final_state, final_times = finals.pop("state"), dict(store.times)
    last = max(store)
    pg = _pg_job(svc.get_job_meta(1), adaptive_enabled=True)
    if rebuild:
        _flush_job_redis(svc)
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: sorted(store))
    assert orch.recover_job(1, pg_job=pg) == last
    gen = int(svc.get_job_meta(1)["generation"])
    assert svc.client.zscore("v2:pending_merges", f"1:{gen}:{last}") is not None
    assert backend.claim_task("w-test", "q", block_ms=10) is None   # nothing to rerun
    drive(w, backend, svc)
    assert svc.get_job_meta(1)["status"] == "COMPLETED"
    assert finals["state"] == final_state
    assert store.times[last] == final_times[last]     # republished sidecar unchanged
    assert svc.client.zscore("v2:pending_merges", f"1:{gen}:{last}") is None


# Recovery after flush-job-redis (meta rebuilt from PostgreSQL) is bitwise.
@pytest.mark.parametrize("method", ["rk4", "grk4_accel"])
def test_flush_job_redis_recovery_is_bitwise(svc, no_pg, monkeypatch, method):
    reference = reference_run(svc, method)
    store = capture_snapshots(monkeypatch)
    orch = create_job(svc, method=method, snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 6)                           # epochs 1 and 2 committed + snapshots
    assert sorted(store) == [1, 2]
    pg = _pg_job(svc.get_job_meta(1))
    _flush_job_redis(svc)
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: sorted(store))
    assert orch.recover_job(1, pg_job=pg) == 2
    drive(w, backend, svc)                             # the gen-0 epoch-3 tasks are skipped
    assert finals["state"] == reference


# A restart reproduces the uninterrupted run bitwise, also with returned
# accelerations (RK4 / Forest-Ruth), and keeps the adaptive step.
@pytest.mark.parametrize("method", ["grk4_accel", "gfr_accel"])
def test_snapshot_restart_is_bitwise_with_returned_accelerations(svc, no_pg, monkeypatch, method):
    store = capture_snapshots(monkeypatch)
    create_job(svc, method=method, snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    reference = finals["state"]
    svc.client.flushdb()
    store.clear()

    orch = create_job(svc, method=method, snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 6)                         # epochs 1 and 2 committed
    assert store[2][2] is not None                    # the shipped a(0) went with the snapshot
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: [1, 2])
    assert orch.recover_job(1) == 2
    for _ in range(3):                                # epoch 3 tasks of generation 0
        backend.ack_task(backend.claim_task("w-test", "q", block_ms=10))
    drive(w, backend, svc)
    assert finals["state"] == reference


def test_snapshot_restart_keeps_the_adaptive_step(svc, no_pg, monkeypatch):
    store = capture_snapshots(monkeypatch)
    adaptive = dict(adaptive_enabled=True, adaptive_eta=0.02, adaptive_dt_min=1e-6, adaptive_dt_max=0.05)
    orch = create_job(svc, snapshot_interval=1, **adaptive)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 3)
    dt2 = backend.claim_task("w-test", "q", block_ms=10).time_step
    assert dt2 != 0.01 and store[1][1] == dt2
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: [1])
    orch.recover_job(1)
    regenerated = []
    for t in iter(lambda: backend.claim_task("w-test", "q", block_ms=10), None):
        if t.generation == 1:
            regenerated.append(t)
        backend.ack_task(t)
    assert regenerated and all(t.time_step == dt2 for t in regenerated)


# ... and for the tree and velocity Verlet with either predictor.
@requires_redis
@pytest.mark.parametrize("method,alg", [("gvv_accel", "direct"), ("gvv_accel", "barnes_hut"),
                                        ("grk4_accel", "barnes_hut"), ("gfr_accel", "barnes_hut"),
                                        ("velocity_verlet", "direct"), ("forest_ruth", "barnes_hut")])
def test_snapshot_restart_is_bitwise(svc, no_pg, monkeypatch, method, alg):
    store = capture_snapshots(monkeypatch)
    create_job(svc, method=method, snapshot_interval=1, force_algorithm=alg)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    reference = finals["state"]
    svc.client.flushdb()
    store.clear()

    orch = create_job(svc, method=method, snapshot_interval=1, force_algorithm=alg)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 6)                         # epochs 1 and 2 committed
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: [1, 2])
    assert orch.recover_job(1) == 2
    for _ in range(3):                                # epoch 3 tasks of generation 0
        backend.ack_task(backend.claim_task("w-test", "q", block_ms=10))
    drive(w, backend, svc)
    assert finals["state"] == reference


# --------------------------------------------------------------------------- #
# The physical clock of adaptive jobs
# --------------------------------------------------------------------------- #

# The commit records the time reached, snapshots carry it, and a recovery
# restores it, so analysis need not assume epoch * dt.


def test_v2_adaptive_clock_is_the_sum_of_the_steps_taken(svc, no_pg, monkeypatch):
    store = capture_snapshots(monkeypatch)
    create_job(svc, snapshot_interval=1, **ADAPTIVE)
    w, backend, finals = v2_worker(svc)
    tasks = run_tasks(w, backend, 9)                 # epochs 1-3, three packages each
    dts = [tasks[3 * k].time_step for k in range(3)]
    assert len(set(dts)) > 1                          # the step adapts
    clock = 0.0
    for epoch, dt in enumerate(dts, start=1):
        clock += dt                                   # the monolithic worker's summation order
        assert store.times[epoch] == (clock, dt)
    assert float(svc.get_job_meta(1)["sim_time"]) == clock
    assert clock != 3 * 0.01                          # what epoch * dt would have claimed


def test_v2_recovery_continues_the_clock_of_the_snapshot(svc, no_pg, monkeypatch):
    store = capture_snapshots(monkeypatch)
    orch = create_job(svc, snapshot_interval=1, **ADAPTIVE)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 6)                         # epochs 1 and 2
    uninterrupted = dict(store.times)
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: [1])
    for t in iter(lambda: backend.claim_task("w-test", "q", block_ms=10), None):
        backend.ack_task(t)                           # drop the in-flight epoch-3 tasks
    assert orch.recover_job(1) == 1
    assert float(svc.get_job_meta(1)["sim_time"]) == uninterrupted[1][0]
    run_tasks(w, backend, 3)                         # epoch 2 again, generation 1
    assert store.times[2] == uninterrupted[2]         # bitwise the same clock
    # a snapshot without a recorded time (written before this change) leaves the clock unknown
    store.times.clear()
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: [2])
    for t in iter(lambda: backend.claim_task("w-test", "q", block_ms=10), None):
        backend.ack_task(t)
    assert orch.recover_job(1) == 2
    assert "sim_time" not in svc.get_job_meta(1)
    run_tasks(w, backend, 3)                         # epoch 3: no clock to continue
    assert store.times[3] == (None, store[2][1])      # time unknown. The step is still recorded


# --------------------------------------------------------------------------- #
# Concurrent recoveries. Cancel and pause during a recovery
# --------------------------------------------------------------------------- #

def _recoverable_job(svc, monkeypatch, **kw):
    """A job with epochs 1 and 2 committed and snapshotted, the epoch-3 tasks
    of generation 0 queued, and recovery pointed at the in-memory snapshots."""
    store = capture_snapshots(monkeypatch)
    orch = create_job(svc, snapshot_interval=1, **kw)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 6)
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: sorted(store))
    return orch, w, backend, finals, store


def _during_download(monkeypatch, action):
    """Run `action` once, inside the recovery's download of the snapshot
    sidecar (before it writes anything)."""
    real, done = fs.download_snapshot_aux, []

    def aux(job_id, epoch):
        if not done:
            done.append(True)
            action()
        return real(job_id, epoch)
    patch_where_used(monkeypatch, fs, "download_snapshot_aux", aux)


def _queued_generations(svc):
    return sorted({int(f[b"generation"]) for _, f in svc.client.xrange(TASKS_STREAM)})


def _process_some(w, backend, n):
    """Claim, process and acknowledge n tasks (stale ones are skipped)."""
    for _ in range(n):
        t = backend.claim_task("w-test", "q", block_ms=10)
        w._process_task(t)
        backend.ack_task(t)


# A second recovery while one runs (a client retry after a proxy timeout on a
# large snapshot download, a double click) is refused. The first one finishes
# and the run stays bitwise.
def test_a_concurrent_recovery_is_refused(svc, no_pg, monkeypatch):
    reference = reference_run(svc)
    orch, w, backend, finals, store = _recoverable_job(svc, monkeypatch)
    refused = []

    def second():
        with pytest.raises(RecoveryConflict, match="already being recovered"):
            orch.recover_job(1)
        refused.append(True)
    _during_download(monkeypatch, second)
    assert orch.recover_job(1) == 2
    assert refused and svc.get_job_meta(1)["generation"] == "1"
    assert not svc.client.exists("v2:job:1:recover_lock")         # released
    drive(w, backend, svc)
    assert finals["state"] == reference


# Recovery A stalls in its download, its lock expires, recovery B runs to the
# end and B's workers register packages of epoch 3. A then must change nothing:
# bumping the generation again, deleting B's counters and enqueue guard and
# rolling the state back under B's generation would stall the job until the
# watchdog failed it.
def test_a_recovery_that_lost_its_lock_changes_nothing(svc, no_pg, monkeypatch):
    reference = reference_run(svc)
    orch, w, backend, finals, store = _recoverable_job(svc, monkeypatch)

    def b_runs():
        svc.client.delete("v2:job:1:recover_lock")              # A's lock expired
        assert orch.recover_job(1) == 2                          # B: generation 1
        _process_some(w, backend, 5)                             # 3 stale + 2 of B's epoch 3
        assert svc.client.hlen("v2:job:1:g1:epoch:3:counted") == 2
    _during_download(monkeypatch, b_runs)
    with pytest.raises(RecoveryConflict, match="outlived its lock"):
        orch.recover_job(1)                                      # A
    meta = svc.get_job_meta(1)
    assert meta["generation"] == "1" and meta["committed_epoch"] == "2"
    assert svc.client.hlen("v2:job:1:g1:epoch:3:counted") == 2   # B's partials kept
    drive(w, backend, svc)
    assert svc.get_job_meta(1)["status"] == "COMPLETED"
    assert finals["state"] == reference


# A cancel always wins over a recovery: before it starts writing, after its
# generation bump, or before its enqueue. The recovery stops, the job stays
# CANCELLED and nothing of the new generation is queued.
@pytest.mark.parametrize("point", ["download", "after_bump", "before_enqueue"])
def test_a_cancel_during_recovery_wins(svc, no_pg, monkeypatch, point):
    orch, w, backend, finals, store = _recoverable_job(svc, monkeypatch)
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: [1])   # a restore would roll back
    state = svc.get_bodies(1)
    if point == "download":
        _during_download(monkeypatch, lambda: orch.cancel_job(1))
    else:
        name = "begin_recovery" if point == "after_bump" else "delete_other_generation_keys"
        real = getattr(svc, name)

        def then_cancel(*a, **k):
            r = real(*a, **k)
            assert orch.cancel_job(1)
            return r
        monkeypatch.setattr(svc, name, then_cancel)
    with pytest.raises(RecoveryConflict, match="cancel"):
        orch.recover_job(1)
    meta = svc.get_job_meta(1)
    assert meta["status"] == "CANCELLED"
    assert meta["generation"] == ("0" if point == "download" else "1")
    if point != "before_enqueue":
        assert svc.get_bodies(1) == state and meta["committed_epoch"] == "2"
    assert _queued_generations(svc) == [0]                       # only the stale epoch-3 tasks
    assert not svc.client.exists("v2:job:1:recover_lock")
    with pytest.raises(RecoveryConflict, match="cancelled"):    # and it is not recovered later
        orch.recover_job(1)


# A pause during a recovery is kept: the recovered epoch runs, the job then
# stays PAUSED with its keys kept without a TTL, and resumes bitwise. (A
# PAUSED job that is recovered is resumed by the recovery.)
def test_a_pause_during_recovery_is_kept(svc, no_pg, monkeypatch):
    reference = reference_run(svc)
    orch, w, backend, finals, store = _recoverable_job(svc, monkeypatch)
    real = svc.begin_recovery

    def then_pause(*a, **k):
        r = real(*a, **k)
        assert orch.pause_job(1)
        return r
    monkeypatch.setattr(svc, "begin_recovery", then_pause)
    assert orch.recover_job(1) == 2
    assert svc.get_job_meta(1)["status"] == "PAUSED"
    assert [svc.client.ttl(k) for k in ("v2:job:1:meta", "v2:bodies:1")] == [-1, -1]
    _process_some(w, backend, 6)                                 # 3 stale + the recovered epoch 3
    assert svc.get_job_meta(1)["committed_epoch"] == "3"
    assert backend.claim_task("w-test", "q", block_ms=10) is None   # paused: epoch 4 not queued
    assert orch.resume_job(1)
    drive(w, backend, svc)
    assert finals["state"] == reference


def test_recovering_a_paused_job_resumes_it(svc, no_pg, monkeypatch):
    orch, w, backend, finals, store = _recoverable_job(svc, monkeypatch)
    assert orch.pause_job(1)
    assert orch.recover_job(1) == 2
    assert svc.get_job_meta(1)["status"] == "IN_PROGRESS"
    assert all(svc.client.ttl(k) > 1000 for k in ("v2:job:1:meta", "v2:bodies:1"))
    drive(w, backend, svc)
    assert svc.get_job_meta(1)["status"] == "COMPLETED"


# An S3 error while listing snapshots is an error, not "no snapshots" (which
# would restart the job from its input): nothing is written.
def test_a_snapshot_listing_error_changes_nothing(svc, no_pg, monkeypatch):
    orch, w, backend, finals, store = _recoverable_job(svc, monkeypatch)
    meta = svc.get_job_meta(1)

    def failing(job_id):
        raise ClientError({"Error": {"Code": "SlowDown", "Message": "busy"}}, "ListObjectsV2")
    monkeypatch.setattr(orchmod, "list_snapshots", failing)
    with pytest.raises(ClientError):
        orch.recover_job(1)
    assert svc.get_job_meta(1) == meta
    assert not svc.client.exists("v2:job:1:recover_lock")


# --------------------------------------------------------------------------- #
# Recovery before the first snapshot: from the input
# --------------------------------------------------------------------------- #

# A job without a snapshot (snapshot_interval=0 writes only the final one)
# restarts from its input, re-read and re-partitioned as on creation, and
# finishes bitwise identical to the uninterrupted run. Also with the meta
# rebuilt from PostgreSQL, and with an adaptive step.
@pytest.mark.parametrize("method,strategy,flush,adaptive", [
    ("rk4", "linear", False, False),
    ("grk4_accel", "random", True, False),
    ("gvv_accel", "spatial", False, True),
    ("rk4", "orb", True, False),
])
def test_recovery_without_a_snapshot_restarts_from_the_input(svc, no_pg, monkeypatch, method, strategy,
                                                             flush, adaptive):
    kw = dict(method=method, partition_strategy=strategy, partition_seed=7, **(ADAPTIVE if adaptive else {}))
    create_job(svc, **kw)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    reference = finals["state"]
    svc.client.flushdb()

    store = capture_snapshots(monkeypatch)
    csv = convert_to_csv_format(initial_bodies())
    reads = []
    patch_where_used(monkeypatch, file_service, "download_file", lambda bucket, key: reads.append((bucket, key)) or csv)
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: sorted(store))
    orch = create_job(svc, **kw)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 5)                          # epoch 1 committed, two packages of epoch 2
    assert not store                                  # interval 0: no snapshot yet
    pg = _pg_job(svc.get_job_meta(1), partition_strategy=strategy, partition_seed=7,
                 integration_method=method)
    if flush:
        _flush_job_redis(svc)
    assert orch.recover_job(1, pg_job=pg) == 0
    assert reads == [("b", "k")]
    meta = svc.get_job_meta(1)
    assert meta["committed_epoch"] == "0" and meta["current_epoch"] == "1"
    assert float(meta["next_dt"]) == 0.01 and "sim_time" not in meta
    assert not svc.client.exists("v2:accelerations:1")
    gen = int(meta["generation"])
    t = None
    while t is None or t.generation != gen:
        if t is not None:
            backend.ack_task(t)                       # stale generation-0 tasks
        t = backend.claim_task("w-test", "q", block_ms=10)
    assert t.epoch_num == 1 and t.time_step == 0.01
    assert w._process_task(t)
    backend.ack_task(t)
    drive(w, backend, svc)
    assert finals["state"] == reference


def test_recovery_without_snapshot_or_input_is_refused(svc, no_pg, monkeypatch):
    orch = create_job(svc)
    svc.update_job_meta(1, "input_file_key", "")
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: [])
    meta = svc.get_job_meta(1)
    assert orch.recover_job(1) is None
    assert svc.get_job_meta(1) == meta


# --------------------------------------------------------------------------- #
# Pause, resume, cancel
# --------------------------------------------------------------------------- #

# Resume racing the merge of the paused epoch must not stall the job.
def test_resume_racing_merge_keeps_the_job_moving(svc, no_pg):
    orch = create_job(svc)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 2)
    orch.pause_job(1)
    last = backend.claim_task("w-test", "q", block_ms=10)
    real_resume = svc.resume_job

    def resume(job_id):                             # just before the status flip
        assert w._process_task(last)                # commits epoch 1, enqueue refused (PAUSED)
        backend.ack_task(last)
        return real_resume(job_id)
    svc.resume_job = resume
    assert orch.resume_job(1)
    svc.resume_job = real_resume
    assert svc.client.xlen(TASKS_STREAM) == 6       # epoch 2 queued by resume
    drive(w, backend, svc)
    assert svc.get_job_meta(1)["status"] == "COMPLETED"


# Pause/resume keeps the adaptive step chosen at commit.
def test_resume_uses_the_committed_next_dt(svc, no_pg):
    adaptive = dict(adaptive_enabled=True, adaptive_eta=0.02, adaptive_dt_min=1e-6, adaptive_dt_max=0.05)
    create_job(svc, **adaptive)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 3)
    dt2 = backend.claim_task("w-test", "q", block_ms=10).time_step
    assert dt2 != 0.01
    svc.client.flushdb()

    orch = create_job(svc, **adaptive)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 2)
    orch.pause_job(1)
    run_tasks(w, backend, 1)                         # commits epoch 1. Enqueue refused
    assert orch.resume_job(1)
    t = backend.claim_task("w-test", "q", block_ms=10)
    assert t.epoch_num == 2 and t.time_step == dt2


# A pause longer than the job TTL loses nothing.
def test_pause_drops_the_ttl_until_resume(svc, no_pg):
    orch = create_job(svc)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 2)
    assert orch.pause_job(1)
    keys = ("v2:job:1:meta", "v2:bodies:1")
    assert [svc.client.ttl(k) for k in keys] == [-1, -1]
    run_tasks(w, backend, 1)                           # the epoch in flight commits while paused
    assert svc.get_job_meta(1)["committed_epoch"] == "1"
    assert [svc.client.ttl(k) for k in keys] == [-1, -1]
    assert orch.resume_job(1)
    assert all(svc.client.ttl(k) > 1000 for k in keys)


def test_accelerations_shipped_while_paused_have_no_ttl(svc, no_pg):
    orch = create_job(svc, method="gvv_accel")
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 2)
    assert orch.pause_job(1)
    run_tasks(w, backend, 1)
    assert svc.client.exists("v2:accelerations:1") and svc.client.ttl("v2:accelerations:1") == -1


def test_resume_without_state_leaves_the_job_paused(svc, no_pg):
    orch = create_job(svc)
    assert orch.pause_job(1)
    svc.client.delete("v2:bodies:1")                    # state lost
    assert orch.resume_job(1) is False
    assert svc.get_job_meta(1)["status"] == "PAUSED"
    svc.client.delete("v2:job:1:meta")
    assert orch.resume_job(1) is False
    assert not svc.client.exists("v2:job:1:meta")


# Pause / resume / cancel never overwrite a final state.
def test_resume_of_a_finished_paused_job_keeps_it_paused(svc, no_pg):
    orch = create_job(svc)
    svc.update_job_meta(1, "committed_epoch", svc.get_job_meta(1)["num_epochs"])
    svc.update_job_meta(1, "status", "PAUSED")
    assert orch.resume_job(1) is False
    assert svc.get_job_meta(1)["status"] == "PAUSED"


@pytest.mark.parametrize("final", ["COMPLETED", "FAILED", "CANCELLED"])
def test_pause_and_cancel_keep_final_states(svc, no_pg, final):
    orch = create_job(svc)
    svc.update_job_meta(1, "status", final)
    assert orch.pause_job(1) is False
    # cancel is idempotent: a cancel retried after its reply was lost in a
    # failover finds its own write and still succeeds
    assert orch.cancel_job(1) is (final == "CANCELLED")
    assert svc.get_job_meta(1)["status"] == final
    assert svc.client.ttl("v2:job:1:meta") > 0


def test_cancel_and_pause_do_not_recreate_a_lost_meta(svc, no_pg):
    orch = create_job(svc)
    svc.client.delete("v2:job:1:meta")
    assert orch.cancel_job(1) is True                   # nothing to stop in Redis
    assert orch.pause_job(1) is False
    assert not svc.client.exists("v2:job:1:meta")


def test_cancel_of_a_paused_job_restores_the_ttl(svc, no_pg):
    orch = create_job(svc)
    assert orch.pause_job(1)
    assert orch.cancel_job(1)
    assert svc.get_job_meta(1)["status"] == "CANCELLED"
    assert svc.client.ttl("v2:job:1:meta") > 0 and svc.client.ttl("v2:bodies:1") > 0


# --------------------------------------------------------------------------- #
# Recording the recovery (PostgreSQL) and the recovery lock
# --------------------------------------------------------------------------- #

def _pending_merges(svc, job_id=1):
    return [m for m in svc.client.zrange(PENDING_MERGES_KEY, 0, -1) if m.decode().startswith(f"{job_id}:")]


# The recovery is recorded (on_restored: the API's PostgreSQL update) before
# any task or pending merge of the new generation exists. Recorded after them,
# a worker could finalize the recovered job first, the completion would be
# skipped (the row still has the old run's completed_at, or is FAILED), and the
# late record would leave the row IN_PROGRESS for good.
def test_a_recovery_is_recorded_before_anything_is_queued(svc, no_pg, monkeypatch):
    store = capture_snapshots(monkeypatch)
    orch = create_job(svc, snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    assert svc.get_job_meta(1)["status"] == "COMPLETED"
    seen = []

    def record(epoch):
        seen.append((epoch, svc.get_job_meta(1)["status"], svc.client.xlen("v2:tasks"),
                     len(_pending_merges(svc))))

    # a finished job: its final epoch is marked pending only after the record
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: sorted(store))
    queued_before = svc.client.xlen("v2:tasks")
    assert orch.recover_job(1, on_restored=record) == N_EPOCHS
    assert seen == [(N_EPOCHS, "IN_PROGRESS", queued_before, 0)]
    assert len(_pending_merges(svc)) == 1
    drive(w, backend, svc)                              # taken over at once: completed again
    assert svc.get_job_meta(1)["status"] == "COMPLETED"

    # a job with epochs left: its tasks are queued only after the record
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: [1])
    queued_before = svc.client.xlen("v2:tasks")
    assert orch.recover_job(1, on_restored=record) == 1
    assert seen[-1] == (1, "IN_PROGRESS", queued_before, 0)
    assert svc.client.xlen("v2:tasks") > queued_before


# A record that refuses the recovery (a cancel seen in PostgreSQL) stops it
# before anything is queued, and frees the lock.
def test_a_recovery_refused_by_its_record_queues_nothing(svc, no_pg, monkeypatch):
    store = capture_snapshots(monkeypatch)
    orch = create_job(svc, snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 3)
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: sorted(store))
    queued_before = svc.client.xlen("v2:tasks")

    def record(epoch):
        raise orchmod.RecoveryConflict("Job 1 was cancelled during its recovery")
    with pytest.raises(orchmod.RecoveryConflict):
        orch.recover_job(1, on_restored=record)
    assert svc.client.xlen("v2:tasks") == queued_before and _pending_merges(svc) == []
    assert not svc.client.exists("v2:job:1:recover_lock")


# The recovery lock has a short TTL, renewed while the recovery runs: a holder
# that died (an API pod killed mid-recovery) frees the job soon, and a live
# recovery longer than the TTL keeps its lock.
def test_the_recovery_lock_expires_after_a_crash_but_not_during_a_long_recovery(svc, no_pg, monkeypatch):
    monkeypatch.setattr(rs, "RECOVERY_LOCK_TTL_S", 0.5)
    monkeypatch.setattr(rs, "RECOVERY_LOCK_RENEW_S", 0.1)
    monkeypatch.setattr(orchmod, "RECOVERY_LOCK_RENEW_S", 0.1)    # its own binding
    assert svc.acquire_recovery_lock(1)                 # a holder that dies without releasing
    assert 0 < svc.client.pttl("v2:job:1:recover_lock") <= 500
    time.sleep(0.7)
    assert not svc.client.exists("v2:job:1:recover_lock")

    store = capture_snapshots(monkeypatch)
    orch = create_job(svc, snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 3)

    def slow_listing(job_id):
        time.sleep(1.5)                                 # three TTLs: kept alive by the renewer
        assert svc.acquire_recovery_lock(1) is None     # still held
        return sorted(store)
    monkeypatch.setattr(orchmod, "list_snapshots", slow_listing)
    assert orch.recover_job(1) == 1
    assert not svc.client.exists("v2:job:1:recover_lock")
    assert not [t for t in __import__("threading").enumerate() if t.name.startswith("recover-lock-")]
