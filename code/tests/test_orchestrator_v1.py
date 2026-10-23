# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The v1 orchestrator completes a job outside the epoch transaction (so the
row lock is not held through the snapshot combination), in an order that a
crash cannot strand: combine, point the job at the result, then delete."""
from __future__ import annotations

import contextlib
import sys
import types

import pytest
from botocore.exceptions import ClientError

import file_service
from helpers import SymbolicColumn, patch_where_used


def _matches(cond, row) -> bool:
    """Evaluate a helpers.SymbolicColumn condition against a row dict."""
    op = cond[0]
    if op == "and":
        return _matches(cond[1], row) and _matches(cond[2], row)
    if op == "eq":
        return row[cond[1]] == cond[2]
    if op == "ne":
        return row[cond[1]] != cond[2]
    if op == "not_in":
        return row[cond[1]] not in cond[2]
    raise AssertionError(f"unexpected condition {cond!r}")


def _fake_job_model(events, row):
    """A one-row Job table: update(...).where(cond).execute() applies the
    fields only if `cond` holds for the row, and returns the matched count."""

    class Job:
        job_id = SymbolicColumn("job_id")
        status = SymbolicColumn("status")

        @staticmethod
        def update(**kw):
            def execute(cond):
                matched = _matches(cond, row)
                if matched:
                    row.update(kw)
                    events.append(("update", kw["status"], kw["results_file_key"]))
                return int(matched)
            return types.SimpleNamespace(where=lambda cond: types.SimpleNamespace(execute=lambda: execute(cond)))
    return Job


def _import_orchestrator(monkeypatch):
    """A fresh import of orchestrator (the module creates its S3 buckets at
    import: not here)."""

    patch_where_used(monkeypatch, file_service, "ensure_buckets_exist", lambda: None)
    sys.modules.pop("orchestrator", None)
    import orchestrator
    return orchestrator


def _run(monkeypatch, combine_ok, error=None, status="IN_PROGRESS"):
    O = _import_orchestrator(monkeypatch)
    events = []
    row = {"job_id": 4, "status": status}
    monkeypatch.setattr(O, "Job", _fake_job_model(events, row))

    def combine(**kw):
        events.append("combine")
        if not combine_ok:
            raise error or RuntimeError("no snapshots")
        return "combined.csv"
    monkeypatch.setattr(O, "combine_epoch_snapshots", combine)
    monkeypatch.setattr(O, "delete_files_by_prefix", lambda *a: events.append("delete") or 3)
    job = types.SimpleNamespace(job_id=4, results_file_key="last-epoch.csv", snapshot_interval=1, num_epochs=5,
                                updated_at=None)
    O._complete_job(job)
    return events


def test_completion_points_at_the_combined_file_before_deleting_snapshots(monkeypatch):
    assert _run(monkeypatch, True) == ["combine", ("update", "COMPLETED", "combined.csv"), "delete"]


def test_a_failed_combination_keeps_the_snapshots(monkeypatch):
    assert _run(monkeypatch, False) == ["combine", ("update", "COMPLETED", "last-epoch.csv")]


def test_a_job_paused_or_cancelled_during_the_combination_keeps_its_snapshots(monkeypatch):
    """The completion is conditional on IN_PROGRESS: a job that left it while
    the snapshots were combined is not completed, and (0 rows updated) its
    snapshots stay for the resume that re-combines them."""
    for status in ("PAUSED", "CANCELLED"):
        assert _run(monkeypatch, True, status=status) == ["combine"]


def test_a_redelivered_epoch_requeues_the_next_one_it_failed_to_publish(monkeypatch):
    """The epoch advance commits before the next epoch's messages are sent. A
    redelivery after a failed publish was acked as a duplicate and the job
    stalled with packages nobody computes."""
    O = _import_orchestrator(monkeypatch)
    job = types.SimpleNamespace(job_id=4, current_epoch=3, num_epochs=10, status="IN_PROGRESS",
                                time_step=0.01, adaptive_enabled=False, save=lambda: None)

    class Col:
        def __eq__(self, other):
            return True

    class Job:
        job_id = Col()

        @staticmethod
        def select():
            return types.SimpleNamespace(where=lambda *a: types.SimpleNamespace(
                for_update=lambda: types.SimpleNamespace(get=lambda: job)))
    monkeypatch.setattr(O, "Job", Job)
    monkeypatch.setattr(O, "db", types.SimpleNamespace(atomic=contextlib.nullcontext))
    queued = []
    monkeypatch.setattr(O, "queue_v1_epoch", lambda j, ch, dt: queued.append((j.current_epoch, dt)))
    acks = []
    ch = types.SimpleNamespace(basic_ack=lambda **k: acks.append(1))
    body = b'{"job_id": 4, "bucket": "epochresults", "file_key": "k", "epoch_num": 3, "a_max": 0.0}'
    # a fresh duplicate (the merger re-publishing after a crash) is only acked:
    # its epoch's successor was published with the first copy
    O.process_finalize_epoch(ch, types.SimpleNamespace(delivery_tag=1, redelivered=False), None, body)
    assert queued == [] and acks == [1]
    # our own redelivery (publish failed, or a crash after the commit) re-queues
    O.process_finalize_epoch(ch, types.SimpleNamespace(delivery_tag=2, redelivered=True), None, body)
    assert queued == [(3, 0.01)] and acks == [1, 1]
    assert job.current_epoch == 3                       # nothing recorded twice


@pytest.mark.parametrize("adaptive", [True, False])
def test_an_epoch_finished_while_paused_records_the_next_step(monkeypatch, adaptive):
    """The resume runs the next epoch with job.next_time_step. Recorded only
    on the IN_PROGRESS path, a paused adaptive job resumed with the step of
    the epoch before."""
    O = _import_orchestrator(monkeypatch)
    job = types.SimpleNamespace(job_id=4, current_epoch=2, num_epochs=10, status="PAUSED", time_step=0.01,
                                adaptive_enabled=adaptive, adaptive_eta=0.02, softening_sq=1.0,
                                adaptive_dt_min=1e-6, adaptive_dt_max=0.05, next_time_step=0.004,
                                results_file_key=None, save=lambda: None)

    class Col:
        def __eq__(self, other):
            return True

    class Job:
        job_id = Col()

        @staticmethod
        def select():
            return types.SimpleNamespace(where=lambda *a: types.SimpleNamespace(
                for_update=lambda: types.SimpleNamespace(get=lambda: job)))
    monkeypatch.setattr(O, "Job", Job)
    monkeypatch.setattr(O, "db", types.SimpleNamespace(atomic=contextlib.nullcontext))
    ch = types.SimpleNamespace(basic_ack=lambda **k: None)
    body = b'{"job_id": 4, "bucket": "epochresults", "file_key": "k", "epoch_num": 3, "a_max": 1e-12}'
    O.process_finalize_epoch(ch, types.SimpleNamespace(delivery_tag=1, redelivered=False), None, body)
    assert job.current_epoch == 3
    assert job.next_time_step == (0.05 if adaptive else 0.004)       # 0.05: a tiny a_max, capped at dt_max


def test_a_transient_s3_error_leaves_the_completion_to_the_redelivery(monkeypatch):
    """Marked COMPLETED after a transient S3 error, the job was never combined:
    the redelivery that resumes a completion requires IN_PROGRESS."""
    slow_down = ClientError({"Error": {"Code": "SlowDown", "Message": "SlowDown"}}, "GetObject")
    with pytest.raises(ClientError):
        _run(monkeypatch, False, error=slow_down)


@pytest.mark.parametrize("a_max,expected", [
    (0.0, 0.01),               # merger could not compute it: keep the current step
    (float("nan"), 1e-6),      # diverged state: the smallest step
    (float("inf"), 1e-6),
    (1e-12, 0.05),             # tiny acceleration: capped at dt_max
])
def test_adaptive_step_of_the_next_epoch(monkeypatch, a_max, expected):
    orchestrator = _import_orchestrator(monkeypatch)
    job = types.SimpleNamespace(job_id=1, time_step=0.01, adaptive_enabled=True, adaptive_eta=0.02,
                                softening_sq=1.0, adaptive_dt_min=1e-6, adaptive_dt_max=0.05)
    assert orchestrator._next_time_step(job, a_max) == expected


def test_a_fixed_step_job_keeps_its_step(monkeypatch):
    orchestrator = _import_orchestrator(monkeypatch)
    job = types.SimpleNamespace(job_id=1, time_step=0.01, adaptive_enabled=False)
    assert orchestrator._next_time_step(job, float("nan")) == 0.01
