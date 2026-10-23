# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The epoch commit: the registrations write each package's result, and its
returned accelerations, into the epoch's staging keys, and the commit renames
them into the state and a(0) (_REGISTER_PARTIAL, _COMMIT_STAGED); the next
epoch's tasks are built from one template (_PUBLISH_EPOCH).

Against a real redis-server (conftest's `svc`); skipped without one.
"""
from __future__ import annotations

import os
import threading
import time
import types

import pytest

import v2.file_service_v2 as fs
import v2.orchestrator_v2 as orchmod
import v2.recovery as recovery
from helpers import (
    N_BODIES,
    N_EPOCHS,
    capture_snapshots,
    create_job,
    drive,
    patch_where_used,
    requires_redis,
    run_tasks,
    set_meta,
    use_models,
    v2_worker,
)
from v2.epoch_tasks import EpochParams, build_epoch_tasks, epoch_task_template
from v2.redis_service import (
    JOB_NOT_RUNNING,
    PARTIAL_REJECTED,
    PENDING_MERGES_KEY,
    STAGING_INCOMPLETE,
    STALE_EPOCH,
    STALE_GENERATION,
    STATE_MISSING,
)
from workers.distributed_worker_v2 import _MergeLeaseKeeper
from workers.fault_state import FROZEN


redis = pytest.importorskip("redis")

pytestmark = requires_redis

BODY, ACCEL = 64, 24


def _state(n):
    return os.urandom(n * BODY)


def _staged_job(svc, n, status="IN_PROGRESS"):
    """A job whose current state (epoch 0) has n bodies."""
    set_meta(svc, 1, status=status)
    svc.client.set("v2:bodies:1", _state(n))


def _register_all(svc, n, package_size, epoch=1, gen=0, accels=False):
    """Register every package of an n-body epoch; returns the results and the
    returned accelerations by package field."""
    p_total = -(-n // package_size)
    parts, accs = {}, {}
    for p in range(1, p_total + 1):
        start, end = (p - 1) * package_size, min(p * package_size, n)
        data = os.urandom((end - start) * BODY)
        acc = os.urandom((end - start) * ACCEL) if accels else None
        parts[f"package_{p}"], accs[f"package_{p}"] = data, acc
        assert svc.register_partial(1, gen, epoch, p, p_total, data, accelerations=acc,
                                    package_size=package_size) == p
    return parts, accs, p_total


def _joined(parts, p_total):
    return b"".join(parts[f"package_{p}"] for p in range(1, p_total + 1))


@pytest.mark.parametrize("n, package_size", [
    (24, 8), (25, 8), (23, 8), (1001, 251), (1001, 63), (7, 7), (7, 1), (10, 3),
])
def test_the_staged_state_is_the_joined_results(svc, n, package_size):
    _staged_job(svc, n)
    parts, accs, p_total = _register_all(svc, n, package_size, accels=True)
    assert svc.client.get("v2:job:1:g0:epoch:1:staging") == _joined(parts, p_total)
    assert svc.client.get("v2:job:1:g0:epoch:1:astaging") == _joined(accs, p_total)
    assert svc.client.hlen("v2:job:1:g0:epoch:1:counted") == p_total
    assert svc.client.hmget("v2:job:1:g0:epoch:1:staged", "s", "a") == [str(p_total).encode()] * 2
    assert svc.client.ttl("v2:job:1:g0:epoch:1:staging") > 0
    code, installed, n_bytes = svc.commit_staged(1, 0, 1, p_total, 0.02, install_accelerations=True)
    assert (code, installed, n_bytes) == (1, True, n * BODY)


def test_a_replayed_registration_is_staged_once(svc):
    _staged_job(svc, 24)
    parts, _, _ = _register_all(svc, 24, 8)
    assert svc.register_partial(1, 0, 1, 2, 3, os.urandom(8 * BODY), package_size=8) == 3
    assert svc.client.hget("v2:job:1:g0:epoch:1:staged", "s") == b"3"
    assert svc.client.getrange("v2:job:1:g0:epoch:1:staging", 8 * BODY, 16 * BODY - 1) == parts["package_2"]


def test_stale_writers_never_touch_the_live_staging_key(svc):
    _staged_job(svc, 24)
    assert svc.register_partial(1, 1, 1, 1, 3, _state(8), package_size=8) == STALE_GENERATION
    assert svc.register_partial(1, 0, 3, 1, 3, _state(8), package_size=8) == STALE_EPOCH
    assert svc.client.keys("v2:job:1:*staging*") == []


def test_a_result_that_does_not_fit_is_refused_without_a_write(svc):
    _staged_job(svc, 24)
    for pkg, data, acc in [(1, _state(7), None),            # one body short
                           (3, _state(9), None),            # past the end of the state
                           (4, _state(8), None),            # no such package
                           (2, _state(8), os.urandom(7 * ACCEL)),   # a(0) rows of the wrong size
                           (2, _state(8)[:-1], None)]:      # not whole bodies
        assert svc.register_partial(1, 0, 1, pkg, 3, data, accelerations=acc,
                                    package_size=8) == PARTIAL_REJECTED
    assert svc.register_partial(1, 0, 1, 1, 3, _state(8), package_size=0) == PARTIAL_REJECTED
    assert svc.client.keys("v2:job:1:g0:*") == []           # nothing written
    svc.client.delete("v2:bodies:1")
    assert svc.register_partial(1, 0, 1, 1, 3, _state(8), package_size=8) == STATE_MISSING


def test_the_staged_commit_renames_the_state_and_writes_the_meta(svc):
    _staged_job(svc, 25)
    parts, _, p_total = _register_all(svc, 25, 8)
    code, installed, n = svc.commit_staged(1, 0, 1, p_total, 0.02, sim_time=0.01, epoch_dt=0.01)
    assert (code, installed, n) == (1, False, 25 * BODY)
    assert svc.get_bodies(1) == _joined(parts, p_total)
    assert svc.client.exists("v2:job:1:g0:epoch:1:staging") == 0
    assert svc.client.ttl("v2:bodies:1") > 0
    meta = svc.get_job_meta(1)
    assert meta["committed_epoch"] == "1" and float(meta["next_dt"]) == 0.02
    assert float(meta["sim_time"]) == 0.01 and float(meta["committed_dt"]) == 0.01
    assert svc.get_publish_marker(1, 0, 1) == 0.02
    # a replay (lost reply) finds the epoch committed and changes nothing
    assert svc.commit_staged(1, 0, 1, p_total, 0.02) == (0, False, 25 * BODY)
    assert svc.get_bodies(1) == _joined(parts, p_total)


def test_an_incomplete_staging_key_is_refused_without_a_write(svc):
    _staged_job(svc, 24)
    before = svc.get_bodies(1)
    svc.register_partial(1, 0, 1, 1, 3, _state(8), package_size=8)
    svc.register_partial(1, 0, 1, 2, 3, _state(8), package_size=8)
    assert svc.commit_staged(1, 0, 1, 3, 0.02)[0] == STAGING_INCOMPLETE
    assert svc.get_bodies(1) == before and svc.get_job_meta(1)["committed_epoch"] == "0"
    assert svc.client.exists("v2:job:1:g0:epoch:1:staging") == 1


def test_the_staged_commit_keeps_the_fence(svc):
    _staged_job(svc, 24)
    _register_all(svc, 24, 8)
    assert svc.commit_staged(1, 1, 1, 3, 0.02)[0] == STALE_GENERATION
    assert svc.commit_staged(1, 0, 3, 3, 0.02)[0] == STALE_EPOCH
    svc.update_job_meta(1, "status", "CANCELLED")
    assert svc.commit_staged(1, 0, 1, 3, 0.02)[0] == JOB_NOT_RUNNING
    assert svc.get_job_meta(1)["committed_epoch"] == "0"


def test_a_paused_job_keeps_its_staged_state_without_a_ttl(svc):
    _staged_job(svc, 24, status="PAUSED")
    _register_all(svc, 24, 8, accels=True)
    code, installed, _ = svc.commit_staged(1, 0, 1, 3, 0.02, install_accelerations=True)
    assert code == 1 and installed
    assert svc.client.ttl("v2:bodies:1") == -1 and svc.client.ttl("v2:accelerations:1") == -1


def test_returned_accelerations_become_a0_in_the_commit(svc):
    _staged_job(svc, 25)
    svc.client.set("v2:accelerations:1", b"old a0")
    _, accs, p_total = _register_all(svc, 25, 8, accels=True)
    code, installed, _ = svc.commit_staged(1, 0, 1, p_total, 0.02, install_accelerations=True)
    assert code == 1 and installed
    assert svc.get_accelerations(1) == _joined(accs, p_total)
    assert svc.get_job_meta(1).get("accel_src_returned") == "1"
    assert svc.client.ttl("v2:accelerations:1") > 0


def test_without_installing_a0_the_previous_one_is_dropped(svc):
    _staged_job(svc, 24)
    svc.client.set("v2:accelerations:1", b"old a0")
    _register_all(svc, 24, 8, accels=True)
    code, installed, _ = svc.commit_staged(1, 0, 1, 3, 0.02, install_accelerations=False)
    assert code == 1 and not installed and svc.get_accelerations(1) is None


def test_incomplete_returned_rows_leave_a0_to_the_merger(svc):
    _staged_job(svc, 24)
    for p in (1, 2, 3):
        acc = os.urandom(8 * ACCEL) if p != 2 else None
        svc.register_partial(1, 0, 1, p, 3, _state(8), accelerations=acc, package_size=8)
    code, installed, _ = svc.commit_staged(1, 0, 1, 3, 0.02, install_accelerations=True)
    assert code == 1 and not installed and svc.get_accelerations(1) is None


def test_the_publication_cleans_up_the_staging_keys(svc):
    _staged_job(svc, 24)
    _register_all(svc, 24, 8, accels=True)
    svc.commit_staged(1, 0, 1, 3, 0.02)          # a(0) not installed: its staging key stays
    assert svc.client.exists("v2:job:1:g0:epoch:1:astaging") == 1
    cleanup, enq, published = svc.publish_epoch(
        1, 0, 1, "tok", 3, 24, 8, ["total_packages", "3"], 0.02, "0.02")
    assert published and cleanup > 0
    assert svc.client.keys("v2:job:1:g0:epoch:1:*") == []


def test_a_lost_commit_of_a_paused_job_leaves_the_epoch_pending(svc):
    """A commit lost in a failover while the job was paused must not end the
    publication: resume would find the epoch's enqueue guard set and queue
    nothing, stalling the job."""
    _staged_job(svc, 24, status="PAUSED")
    _register_all(svc, 24, 8)                   # epoch 1 pending, its commit then lost
    svc.client.set("v2:job:1:merge_lease:g0:1", "tok")
    cleanup, enq, published = svc.publish_epoch(
        1, 0, 1, "tok", 3, 24, 8, ["total_packages", "3"], 0.02, "0.02")
    assert not published and cleanup < 0
    assert svc.client.zscore(PENDING_MERGES_KEY, "1:0:1") is not None
    assert svc.client.get("v2:job:1:merge_lease:g0:1") == b"tok"
    assert svc.client.hget("v2:job:1:g0:epoch:1:staged", "s") == b"3"


# --------------------------------------------------------------------------- #
# whole jobs
# --------------------------------------------------------------------------- #

def _run(svc, method, **kw):
    svc.client.flushdb()
    create_job(svc, method=method, **kw)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    assert svc.get_job_meta(1)["status"] == "COMPLETED"
    return finals["state"], svc.client.keys("v2:job:1:*stag*")


@pytest.mark.parametrize("method", ["symplectic_euler", "rk4", "grk4_accel", "gvv_accel"])
def test_jobs_complete_deterministically_and_leave_no_staging_keys(svc, no_pg, method):
    first, left = _run(svc, method)
    again, _ = _run(svc, method)
    assert first == again and len(first) == N_BODIES * BODY
    assert left == []


def test_the_state_is_read_back_only_for_snapshots(svc, no_pg, monkeypatch):
    """The merger GETs the committed state only on the epochs that need it
    (snapshot epochs and the final one), and their snapshots get it."""
    uploads = []
    patch_where_used(monkeypatch, fs, "upload_snapshot_async",
                        lambda job_id, epoch, state, **k: uploads.append((epoch, len(state))))
    _run(svc, "rk4", snapshot_interval=2)
    assert uploads == [(e, N_BODIES * BODY) for e in range(2, N_EPOCHS + 1, 2)]


def test_a_job_is_recovered_bitwise(svc, no_pg, monkeypatch):
    """A job restarted from its epoch-2 snapshot under a new generation ends
    as the uninterrupted run (the staging keys are generation-scoped, so
    nothing of generation 0 is reused)."""
    store = capture_snapshots(monkeypatch)
    create_job(svc, method="grk4_accel", snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    reference = finals["state"]
    svc.client.flushdb()
    store.clear()

    orch = create_job(svc, method="grk4_accel", snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 6)                         # epochs 1 and 2 committed
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: [1, 2])
    assert orch.recover_job(1) == 2
    for _ in range(3):                                # epoch 3 tasks of generation 0
        backend.ack_task(backend.claim_task("w-test", "q", block_ms=10))
    drive(w, backend, svc)
    assert finals["state"] == reference
    assert svc.client.keys("v2:job:1:*stag*") == []


class _JobRow:
    """A one-row Job model for the recovery's bookkeeping: get_or_none gives
    the row, updates apply to it."""

    class _Col:
        def __init__(self, name):
            self.name = name

        def __eq__(self, other):
            return self

        def __and__(self, other):
            return self

        def __add__(self, other):
            return ("add", self.name, other)

        def not_in(self, values):
            return self

    job_id, status, recovery_count = _Col("job_id"), _Col("status"), _Col("recovery_count")

    def __init__(self):
        self.row = types.SimpleNamespace(job_id=1, status="IN_PROGRESS", recovery_count=0)

    def get_or_none(self, *a):
        return self.row

    def update(self, **values):
        row = self.row

        class _Q:
            def where(self, *a):
                return self

            def execute(self):
                for k, v in values.items():
                    setattr(row, k, getattr(row, v[1]) + v[2] if isinstance(v, tuple) else v)
                return 1
        return _Q()


def _lose_the_staged_state(svc, monkeypatch, w, backend, epoch):
    """Register every package of `epoch` with the merger "dead", then lose the
    epoch's staging key (a failover that lost writes not yet on the replica)."""
    with monkeypatch.context() as m:
        m.setattr(type(w), "_merge_and_advance", lambda self, t, rs: None)
        run_tasks(w, backend, 3)
    svc.client.delete(f"v2:job:1:g0:epoch:{epoch}:staging")
    time.sleep(0.01)


def test_a_lost_staged_state_recovers_the_job_bitwise(svc, no_pg, monkeypatch):
    """The epoch cannot be committed from anything else, but the job's latest
    snapshot is intact: the merger recovers the job from it under a new
    generation (as the recover endpoint would), and the job ends as the
    uninterrupted run. A second merger that finds the same loss leaves the job
    to that recovery."""
    store = capture_snapshots(monkeypatch)
    create_job(svc, snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    reference = finals["state"]
    svc.client.flushdb()
    store.clear()

    jobs = _JobRow()
    use_models(monkeypatch, types.SimpleNamespace(Job=jobs, utcnow=lambda: None))
    orch = create_job(svc, snapshot_interval=1)
    monkeypatch.setattr(recovery, "get_orchestrator", lambda: orch)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 3)                         # epoch 1 committed, its snapshot taken
    _lose_the_staged_state(svc, monkeypatch, w, backend, 2)
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: sorted(store))
    w._try_takeover()
    assert svc.get_job_meta(1)["generation"] == "1"
    assert jobs.row.recovery_count == 1 and jobs.row.current_epoch == 1
    # the other merger of generation 0: no second recovery
    assert w._recover_lost_epoch(types.SimpleNamespace(job_id=1, generation=0), "lost")
    assert svc.get_job_meta(1)["generation"] == "1"
    drive(w, backend, svc)
    assert finals["state"] == reference


def test_a_lost_staged_state_fails_the_job_when_it_cannot_be_recovered(svc, no_pg, monkeypatch):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    _lose_the_staged_state(svc, monkeypatch, w, backend, 1)
    failed = []
    monkeypatch.setattr(type(w), "_fail_job", lambda self, task, reason: failed.append(reason) or True)

    def unrecoverable(*a, **k):
        raise ConnectionError("object store unreachable")
    patch_where_used(monkeypatch, recovery, "recover_v2_job", unrecoverable)
    w._try_takeover()
    assert failed and "staged state incomplete" in failed[0]
    assert svc.get_job_meta(1)["committed_epoch"] == "0"
    assert svc.get_pending_merges() == []


# --------------------------------------------------------------------------- #
# the next epoch's tasks are built from one template
# --------------------------------------------------------------------------- #

def _stream_entries(svc, stream="v2:tasks"):
    return [{k.decode(): v.decode() for k, v in fields.items()}
            for _id, fields in svc.client.xrange(stream)]


@pytest.mark.parametrize("n, package_size", [(20001, 1251), (1001, 63), (25, 8)])
def test_publication_enqueues_the_tasks_the_orchestrator_builds(svc, n, package_size):
    """publish_epoch writes exactly the entries the orchestrator enqueues
    from the full tasks (build_epoch_tasks) for the same epoch."""
    p_total = -(-n // package_size)
    params = EpochParams(package_size=package_size, total_packages=p_total, num_epochs=1000,
                         snapshot_interval=50, force_algorithm="direct", barnes_hut_theta=0.5,
                         softening_sq=1.0, integration_method="grk4_accel", adaptive_enabled=False,
                         adaptive_eta=0.02, adaptive_dt_min=1e-10, adaptive_dt_max=0.000125,
                         gpu_enabled=False)
    set_meta(svc, 1, committed_epoch="1", current_epoch="1")
    tpl = epoch_task_template(1, 0, 2, n, 0.000125, params)
    result = svc.publish_epoch(1, 0, 1, "tok", p_total, n, package_size, tpl,
                               0.000125, str(0.000125))
    assert result[1] == p_total and result[2]
    assert svc.get_job_meta(1)["current_epoch"] == "2"
    full = [{k: str(v) for k, v in t.items()}
            for t in build_epoch_tasks(1, 0, 2, n, 0.000125, params)]
    assert _stream_entries(svc) == full


# --------------------------------------------------------------------------- #
# the merge lease keeper (no renewal thread per merge)
# --------------------------------------------------------------------------- #

class _FakeLeases:
    def __init__(self, answer=True):
        self.calls, self.answer = 0, answer

    def renew_merge_lease(self, *a):
        self.calls += 1
        return self.answer


def test_a_short_merge_renews_nothing_and_starts_no_thread_of_its_own():
    keeper, rs = _MergeLeaseKeeper(), _FakeLeases()
    keeper.hold(rs, 1, 0, 1, "t0", 30_000).stop()             # starts the keeper's thread
    threads = threading.active_count()
    for e in range(2, 50):
        keeper.hold(rs, 1, 0, e, f"t{e}", 30_000).stop()
    assert threading.active_count() == threads and rs.calls == 0


def test_a_long_merge_is_renewed_until_stopped():
    keeper, rs = _MergeLeaseKeeper(), _FakeLeases()
    lease = keeper.hold(rs, 1, 0, 1, "t", 300)                # renewed every 0.1 s
    time.sleep(0.35)
    lease.stop()
    calls = rs.calls
    assert calls >= 2 and not lease.lost
    time.sleep(0.25)
    assert rs.calls == calls                                  # none after stop()


def test_a_refused_renewal_marks_the_lease_lost_and_a_freeze_renews_nothing():
    keeper = _MergeLeaseKeeper()
    taken = _FakeLeases(answer=False)
    lease = keeper.hold(taken, 1, 0, 1, "t", 300)
    time.sleep(0.2)
    assert lease.lost and taken.calls == 1
    frozen = _FakeLeases()
    FROZEN.set()
    try:
        lease = keeper.hold(frozen, 1, 0, 2, "t", 300)
        time.sleep(0.3)
        assert frozen.calls == 0
    finally:
        FROZEN.clear()
        lease.stop()
