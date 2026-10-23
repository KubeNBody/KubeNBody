# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The V2 job API (v2/api_v2.py): recover / resume / cancel endpoints over an
in-memory Job table, and the full-history export."""
from __future__ import annotations

import csv as _csv
import datetime
import io as _io
import sys
import threading
import time
import types

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

import v2.api_v2 as A
import v2.binary_format as BF
import v2.file_service_v2 as FS
import v2.file_service_v2 as fs
import v2.orchestrator_v2 as orchmod
from helpers import (
    call_route,
    capture_snapshots,
    create_job,
    patch_where_used,
    requires_redis,
    run_tasks,
    use_models,
    v2_worker,
)
from v2.orchestrator_v2 import RecoveryConflict


class _Cond:
    def __init__(self, fn):
        self.fn = fn

    def __and__(self, other):
        return _Cond(lambda r: self.fn(r) and other.fn(r))


class _Col:
    """Column of the in-memory Job table below (peewee expression subset)."""

    def __init__(self, name):
        self.name = name

    __hash__ = object.__hash__

    def __eq__(self, value):
        return _Cond(lambda r: getattr(r, self.name) == value)

    def in_(self, values):
        return _Cond(lambda r: getattr(r, self.name) in values)

    def not_in(self, values):
        return _Cond(lambda r: getattr(r, self.name) not in values)

    def __add__(self, n):
        return _Cond(lambda r: getattr(r, self.name) + n)


def _job_table(row, columns):
    """A Job model over one row: get_or_none, and update().where().execute()
    applying the update only when the condition holds (returns rows changed)."""
    class _Update:
        def __init__(self, values):
            self.values, self.cond = values, None

        def where(self, cond):
            self.cond = cond
            return self

        def execute(self):
            if not self.cond.fn(row):
                return 0
            new = {k: v.fn(row) if isinstance(v, _Cond) else v for k, v in self.values.items()}
            for k, v in new.items():
                setattr(row, k, v)
            return 1

    attrs = {c: _Col(c) for c in columns}
    attrs["get_or_none"] = staticmethod(lambda *a, **k: row)
    attrs["update"] = staticmethod(lambda **values: _Update(values))
    return type("Job", (), attrs)


@pytest.fixture
def api(monkeypatch):
    """The v2 API module, importable without PostgreSQL (models stubbed)."""

    class _Models(types.ModuleType):
        def __getattr__(self, name):
            return type(name, (), {})
    if "v2.api_v2" not in sys.modules:
        use_models(monkeypatch, _Models("models"))
    # recovery drops a completed job's persisted metrics from S3. No S3 here
    import services.comparison_cache as cache
    import v2.api_v2 as api_mod
    api_mod.dropped_metrics = []
    patch_where_used(monkeypatch, cache, "delete_persistent_metrics", lambda job_id: api_mod.dropped_metrics.append(job_id) or 0)
    return api_mod


def _api_row(api, status, **extra):
    fields = dict(job_id=1, job_type=api.JobType.DISTRIBUTED_V2.value, status=status,
                  current_epoch=0, worker_id=None, leased_at=None, updated_at=None,
                  recovery_count=0, completed_at=None)
    fields.update(extra)
    return types.SimpleNamespace(**fields)


def _call(result):
    return call_route(result)


def _recovers_to(epoch, before=None):
    """A fake orchestrator.recover_job restoring `epoch`: it records the
    recovery through on_restored, as the real one does before queueing."""
    def recover(job_id, pg_job=None, on_restored=None, if_generation=None):
        if before:
            before()
        if epoch is not None and on_restored is not None:
            on_restored(epoch)
        return epoch
    return recover


_JOB_COLUMNS = ("job_id", "status", "current_epoch", "updated_at", "worker_id", "leased_at",
                "recovery_count", "completed_at")


def _patch(monkeypatch, api, name, value):
    """Patch `name` in the API module and in v2.recovery, which the recover
    endpoint runs the recovery through."""
    monkeypatch.setattr(api, name, value)
    monkeypatch.setattr(sys.modules["v2.recovery"], name, value)


# --------------------------------------------------------------------------- #
# Recover
# --------------------------------------------------------------------------- #

def test_recover_endpoint_counts_recoveries(api, monkeypatch):
    row = _api_row(api, api.JobStatus.FAILED.value, recovery_count=2)
    _patch(monkeypatch, api, "Job", _job_table(row, _JOB_COLUMNS))
    _patch(monkeypatch, api, "get_orchestrator",
                        lambda: types.SimpleNamespace(recover_job=_recovers_to(3)))
    assert _call(api.recover_job(1, current_user=None)).recovered_epoch == 3
    assert row.status == api.JobStatus.IN_PROGRESS.value and row.current_epoch == 3
    assert row.recovery_count == 3


# Recovering a COMPLETED job needs force, and its rerun completes again.
def test_recover_endpoint_refuses_completed_unless_forced(api, monkeypatch):
    calls = []
    row = _api_row(api, api.JobStatus.COMPLETED.value, recovery_count=0,
                   completed_at=datetime.datetime(2026, 1, 1))
    _patch(monkeypatch, api, "Job", _job_table(row, _JOB_COLUMNS))
    _patch(monkeypatch, api, "get_orchestrator", lambda: types.SimpleNamespace(
        recover_job=_recovers_to(2, before=lambda: calls.append(1))))
    with pytest.raises(api.HTTPException) as exc:
        _call(api.recover_job(1, current_user=None))
    assert exc.value.status_code == 409 and calls == []
    assert row.status == api.JobStatus.COMPLETED.value
    _call(api.recover_job(1, force=True, current_user=None))
    assert calls == [1] and row.status == api.JobStatus.IN_PROGRESS.value
    assert row.completed_at is None and row.recovery_count == 1
    deadline = time.time() + 5                        # dropped in a background thread
    while not api.dropped_metrics and time.time() < deadline:
        time.sleep(0.01)
    assert api.dropped_metrics == [1]                 # the completed run's persisted metrics
    assert not [t for t in threading.enumerate() if t.name.startswith("drop-metrics-") and t.is_alive()]


def test_recover_endpoint_without_snapshots_is_404(api, monkeypatch):
    row = _api_row(api, api.JobStatus.FAILED.value)
    _patch(monkeypatch, api, "Job", _job_table(row, _JOB_COLUMNS))
    _patch(monkeypatch, api, "get_orchestrator",
                        lambda: types.SimpleNamespace(recover_job=_recovers_to(None)))
    with pytest.raises(api.HTTPException) as exc:
        _call(api.recover_job(1, current_user=None))
    assert exc.value.status_code == 404


# A concurrent recovery or a cancel (409), an object-store failure (503, not
# "no snapshots"): the row is left as it was.
@pytest.mark.parametrize("error,code", [("conflict", 409), ("s3", 503), ("endpoint", 503)])
def test_recover_endpoint_errors(api, monkeypatch, error, code):
    exc_of = {"conflict": RecoveryConflict("Job 1 is already being recovered"),
              "s3": ClientError({"Error": {"Code": "SlowDown", "Message": "busy"}}, "ListObjectsV2"),
              "endpoint": EndpointConnectionError(endpoint_url="http://minio:9000")}

    def recover(job_id, pg_job=None, on_restored=None, if_generation=None):
        raise exc_of[error]
    row = _api_row(api, api.JobStatus.FAILED.value)
    _patch(monkeypatch, api, "Job", _job_table(row, _JOB_COLUMNS))
    _patch(monkeypatch, api, "get_orchestrator", lambda: types.SimpleNamespace(recover_job=recover))
    with pytest.raises(api.HTTPException) as exc:
        _call(api.recover_job(1, current_user=None))
    assert exc.value.status_code == code
    assert row.status == api.JobStatus.FAILED.value and row.updated_at is None


def test_recover_endpoint_refuses_a_cancelled_job(api, monkeypatch):
    row = _api_row(api, api.JobStatus.CANCELLED.value)
    _patch(monkeypatch, api, "Job", _job_table(row, _JOB_COLUMNS))
    _patch(monkeypatch, api, "get_orchestrator", lambda: types.SimpleNamespace(
        recover_job=lambda job_id, pg_job=None: pytest.fail("must not recover")))
    with pytest.raises(api.HTTPException) as exc:
        _call(api.recover_job(1, current_user=None))
    assert exc.value.status_code == 409 and row.status == api.JobStatus.CANCELLED.value


# A cancel or pause that lands after Redis is recovered, before the row is
# written, is kept in PostgreSQL. A cancel is also (re)applied to Redis (it
# may have found no meta while the recovery rebuilt it).
@pytest.mark.parametrize("meanwhile", ["CANCELLED", "PAUSED"])
def test_recover_endpoint_keeps_a_status_changed_during_the_recovery(api, monkeypatch, meanwhile):
    row = _api_row(api, api.JobStatus.IN_PROGRESS.value)
    cancels = []

    _patch(monkeypatch, api, "Job", _job_table(row, _JOB_COLUMNS))
    _patch(monkeypatch, api, "get_orchestrator", lambda: types.SimpleNamespace(
        recover_job=_recovers_to(2, before=lambda: setattr(row, "status", meanwhile)),
        cancel_job=lambda job_id: cancels.append(job_id) or True))
    if meanwhile == "CANCELLED":
        with pytest.raises(api.HTTPException) as exc:
            _call(api.recover_job(1, current_user=None))
        assert exc.value.status_code == 409 and cancels == [1]
    else:
        assert _call(api.recover_job(1, current_user=None)).recovered_epoch == 2
        assert cancels == []
    assert row.status == meanwhile


def test_recover_endpoint_resumes_a_paused_job(api, monkeypatch):
    row = _api_row(api, api.JobStatus.PAUSED.value)
    _patch(monkeypatch, api, "Job", _job_table(row, _JOB_COLUMNS))
    _patch(monkeypatch, api, "get_orchestrator", lambda: types.SimpleNamespace(
        recover_job=_recovers_to(0)))
    assert "its input" in _call(api.recover_job(1, current_user=None)).message
    assert row.status == api.JobStatus.IN_PROGRESS.value and row.current_epoch == 0


# End to end: a cancel through the API while the recovery downloads its
# snapshot wins in Redis and PostgreSQL.
@requires_redis
def test_cancel_endpoint_during_a_recovery_wins(svc, no_pg, api, monkeypatch):
    store = capture_snapshots(monkeypatch)
    orch = create_job(svc, snapshot_interval=1)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 3)
    monkeypatch.setattr(orchmod, "list_snapshots", lambda j: sorted(store))
    row = _api_row(api, api.JobStatus.IN_PROGRESS.value)
    _patch(monkeypatch, api, "Job", _job_table(row, _JOB_COLUMNS))
    _patch(monkeypatch, api, "get_orchestrator", lambda: orch)
    real_aux = fs.download_snapshot_aux

    def aux(job_id, epoch):
        _call(api.cancel_job(1, current_user=None))
        return real_aux(job_id, epoch)
    patch_where_used(monkeypatch, fs, "download_snapshot_aux", aux)
    with pytest.raises(api.HTTPException) as exc:
        _call(api.recover_job(1, current_user=None))
    assert exc.value.status_code == 409
    assert row.status == api.JobStatus.CANCELLED.value
    assert svc.get_job_meta(1)["status"] == "CANCELLED"


# --------------------------------------------------------------------------- #
# Resume and cancel
# --------------------------------------------------------------------------- #

@requires_redis
def test_resume_endpoint_keeps_paused_when_redis_cannot_resume(svc, no_pg, api, monkeypatch):
    orch = create_job(svc)
    assert orch.pause_job(1)
    svc.client.delete("v2:job:1:meta", "v2:bodies:1")   # as if expired during the pause
    row = _api_row(api, api.JobStatus.PAUSED.value)
    _patch(monkeypatch, api, "Job", _job_table(row, _JOB_COLUMNS))
    _patch(monkeypatch, api, "get_orchestrator", lambda: orch)
    with pytest.raises(api.HTTPException) as exc:
        _call(api.resume_job(1, current_user=None))
    assert exc.value.status_code == 409
    assert row.status == api.JobStatus.PAUSED.value


@requires_redis
def test_resume_endpoint_resumes(svc, no_pg, api, monkeypatch):
    orch = create_job(svc)
    w, backend, finals = v2_worker(svc)
    run_tasks(w, backend, 3)
    assert orch.pause_job(1)
    row = _api_row(api, api.JobStatus.PAUSED.value, worker_id="old")
    _patch(monkeypatch, api, "Job", _job_table(row, _JOB_COLUMNS))
    _patch(monkeypatch, api, "get_orchestrator", lambda: orch)
    _call(api.resume_job(1, current_user=None))
    assert row.status == api.JobStatus.IN_PROGRESS.value and row.worker_id is None
    assert svc.get_job_meta(1)["status"] == "IN_PROGRESS"


@requires_redis
def test_cancel_endpoint_never_overwrites_a_final_state(svc, no_pg, api, monkeypatch):
    orch = create_job(svc)
    svc.update_job_meta(1, "status", "COMPLETED")       # completion not yet in PostgreSQL
    row = _api_row(api, api.JobStatus.IN_PROGRESS.value)
    _patch(monkeypatch, api, "Job", _job_table(row, _JOB_COLUMNS))
    _patch(monkeypatch, api, "get_orchestrator", lambda: orch)
    with pytest.raises(api.HTTPException) as exc:
        _call(api.cancel_job(1, current_user=None))
    assert exc.value.status_code == 409
    assert row.status == api.JobStatus.IN_PROGRESS.value
    assert svc.get_job_meta(1)["status"] == "COMPLETED"
    with pytest.raises(api.HTTPException) as exc:
        _call(api.pause_job(1, current_user=None))
    assert exc.value.status_code == 409
    assert row.status == api.JobStatus.IN_PROGRESS.value


@requires_redis
def test_cancel_endpoint_cancels_a_running_job(svc, no_pg, api, monkeypatch):
    orch = create_job(svc)
    row = _api_row(api, api.JobStatus.IN_PROGRESS.value)
    _patch(monkeypatch, api, "Job", _job_table(row, _JOB_COLUMNS))
    _patch(monkeypatch, api, "get_orchestrator", lambda: orch)
    _call(api.cancel_job(1, current_user=None))
    assert row.status == api.JobStatus.CANCELLED.value
    assert svc.get_job_meta(1)["status"] == "CANCELLED"


# --------------------------------------------------------------------------- #
# Full-history export
# --------------------------------------------------------------------------- #

# It includes the final state (written only as final_results.csv) and fails
# instead of skipping an unreadable snapshot.


def _history(monkeypatch, snapshots, final_epoch=None, unreadable=()):
    def body(epoch, i):
        return {"n": i, "idBody": i, "m": 1.0, "x": float(epoch), "y": 0.0, "z": 0.0, "vx": 0.0, "vy": 0.0, "vz": 0.0}

    def download(job_id, epoch):
        if epoch in unreadable:
            raise OSError("timeout")
        return BF.bodies_to_binary([body(epoch, 1), body(epoch, 2)])
    final = ("epoch,n,idBody,m,x,y,z,vx,vy,vz\n" +
             "".join(f"{final_epoch},{i},{i},1.0,{final_epoch},0,0,0,0,0\n" for i in (1, 2))) if final_epoch else None

    class S3:
        def get_object(self, Bucket, Key):
            if final is None:
                raise FileNotFoundError(Key)
            return {"Body": _io.BytesIO(final.encode())}
    patch_where_used(monkeypatch, FS, "list_snapshots", lambda job_id: list(snapshots))
    patch_where_used(monkeypatch, FS, "download_snapshot", download)
    patch_where_used(monkeypatch, FS, "get_s3_client", lambda: S3())
    return A


def test_full_history_includes_the_final_state_once(monkeypatch):
    A = _history(monkeypatch, [2, 4], final_epoch=5)
    rows = list(_csv.DictReader(_io.StringIO("".join(A.iter_full_history_csv(9, completed=True)))))
    assert [int(r["epoch"]) for r in rows] == [2, 2, 4, 4, 5, 5]
    # a final-only job (snapshot_interval 0) exports its final state
    A = _history(monkeypatch, [], final_epoch=5)
    assert "".join(A.iter_full_history_csv(9, completed=True)).count("\n5,") == 2
    # a running job has no final state yet. Nothing at all is a 404
    A = _history(monkeypatch, [])
    with pytest.raises(LookupError):
        "".join(A.iter_full_history_csv(9, completed=False))


def test_full_history_fails_instead_of_leaving_out_a_snapshot(monkeypatch):
    A = _history(monkeypatch, [2, 4], final_epoch=5, unreadable={4})
    with pytest.raises(A.IncompleteHistoryError, match=r"\[4\]"):
        "".join(A.iter_full_history_csv(9, completed=True))
    A = _history(monkeypatch, [2, 4])                    # completed, but the final CSV is missing
    with pytest.raises(A.IncompleteHistoryError, match="final state"):
        "".join(A.iter_full_history_csv(9, completed=True))


def test_full_history_is_byte_identical_to_the_dict_writer_export(monkeypatch):
    """The export, built from final_results_csv frames, has the bytes
    csv.DictWriter writes from per-body dicts."""
    A = _history(monkeypatch, [2, 4], final_epoch=5)
    out = _io.StringIO()
    w = _csv.DictWriter(out, fieldnames=A.HISTORY_FIELDS)
    w.writeheader()
    for epoch in (2, 4):
        for i in (1, 2):
            w.writerow({"epoch": epoch, "n": i, "idBody": i, "m": 1.0, "x": float(epoch), "y": 0.0,
                        "z": 0.0, "vx": 0.0, "vy": 0.0, "vz": 0.0})
    for i in (1, 2):
        w.writerow(dict(zip(A.HISTORY_FIELDS, f"5,{i},{i},1.0,5,0,0,0,0,0".split(","))))
    assert "".join(A.iter_full_history_csv(9, completed=True)) == out.getvalue()
