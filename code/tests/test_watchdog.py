# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The watchdog's handling of distributed (V2) jobs: stall detection,
reconciling terminal Redis states into PostgreSQL, and the republication of
monolithic jobs through a single message builder."""
from __future__ import annotations

import ast
import dataclasses
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import peewee as pw
import pytest

from helpers import bump_generation, import_watchdog, requires_redis, set_meta
from schemas import MonolithicJobMessage


T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class _Field:
    """A column, or a condition on columns (`job_id == 7` is remembered)."""
    def __init__(self, name=None, eq=None):
        self.name, self.eq = name, dict(eq or {})

    def __eq__(self, other):
        return _Field(eq={self.name: other})

    def __and__(self, other):
        return _Field(eq={**self.eq, **other.eq})

    def in_(self, values):
        return _Field()

    def is_null(self):
        return _Field()


class _Query:
    def __init__(self, table, update=None):
        self.table, self.update, self.job_id = table, update, None

    def where(self, *a):
        self.job_id = a[0].eq.get("job_id") if a and isinstance(a[0], _Field) else None
        return self

    def __iter__(self):
        return iter([j for j in self.table if j.status == "IN_PROGRESS"])

    def execute(self):
        # conditional update: only rows still IN_PROGRESS (the test uses one job)
        n = 0
        for j in self.table:
            if j.status == "IN_PROGRESS" and self.job_id in (None, j.job_id):
                j.status, n = self.update["status"], n + 1
        return n


@pytest.fixture
def watchdog(monkeypatch):
    table = []

    class Job:
        job_id, status, job_type, updated_at = (_Field("job_id"), _Field("status"), _Field("job_type"),
                                                _Field("updated_at"))

        @staticmethod
        def select():
            return _Query(table)

        @staticmethod
        def update(**kw):
            return _Query(table, kw)

    wd = import_watchdog(monkeypatch, Job, db=None, utcnow=lambda: T0)
    yield wd, table
    sys.modules.pop("watchdog", None)


def _job(job_id, updated=T0 - timedelta(hours=1)):
    return types.SimpleNamespace(job_id=job_id, status="IN_PROGRESS", job_type="distributed_v2",
                                 updated_at=updated, created_at=updated)


# --------------------------------------------------------------------------- #
# Stalled V2 jobs
# --------------------------------------------------------------------------- #

@requires_redis
def test_v2_job_without_progress_is_marked_failed(svc, watchdog):
    wd, table = watchdog
    table.append(_job(1))
    set_meta(svc, 1, committed_epoch=5)
    tracker = wd.V2StallTracker()
    assert wd.detect_stalled_v2_jobs(svc, tracker, now=T0) == []                     # first sight
    assert wd.detect_stalled_v2_jobs(svc, tracker, now=T0 + timedelta(minutes=10)) == []
    svc.update_job_meta(1, "committed_epoch", 6)                                        # progress
    assert wd.detect_stalled_v2_jobs(svc, tracker, now=T0 + timedelta(minutes=35)) == []
    stalled = wd.detect_stalled_v2_jobs(svc, tracker, now=T0 + timedelta(minutes=70))
    assert [s[0] for s in stalled] == [1]
    assert wd.handle_stalled_v2_jobs(stalled, svc) == 1
    assert table[0].status == "FAILED" and svc.get_job_meta(1)["status"] == "FAILED"


@requires_redis
def test_v2_job_with_lost_state_is_marked_failed_after_grace(svc, watchdog):
    wd, table = watchdog
    table.append(_job(2, updated=T0 - timedelta(minutes=3)))
    tracker = wd.V2StallTracker()
    assert wd.detect_stalled_v2_jobs(svc, tracker, now=T0) == []                      # grace
    stalled = wd.detect_stalled_v2_jobs(svc, tracker, now=T0 + timedelta(minutes=15))
    assert stalled == [(2, "Redis state lost (no job meta)", None)]
    assert wd.handle_stalled_v2_jobs(stalled, svc) == 1 and table[0].status == "FAILED"
    assert not svc.client.exists("v2:job:2:meta")                                       # not recreated


@requires_redis
def test_paused_v2_job_is_not_stalled_and_restarts_its_clock(svc, watchdog):
    wd, table = watchdog
    table.append(_job(3))
    set_meta(svc, 3, committed_epoch=5)
    tracker = wd.V2StallTracker()
    wd.detect_stalled_v2_jobs(svc, tracker, now=T0)
    svc.update_job_meta(3, "status", "PAUSED")
    assert wd.detect_stalled_v2_jobs(svc, tracker, now=T0 + timedelta(hours=5)) == []
    svc.update_job_meta(3, "status", "IN_PROGRESS")                                     # resumed
    assert wd.detect_stalled_v2_jobs(svc, tracker, now=T0 + timedelta(hours=5, minutes=1)) == []


@requires_redis
def test_stall_that_resolves_before_marking_is_left_alone(svc, watchdog):
    """The fenced fail refuses a generation that changed (a recovery) meanwhile."""
    wd, table = watchdog
    table.append(_job(4))
    set_meta(svc, 4, committed_epoch=5)
    tracker = wd.V2StallTracker()
    wd.detect_stalled_v2_jobs(svc, tracker, now=T0)
    stalled = wd.detect_stalled_v2_jobs(svc, tracker, now=T0 + timedelta(minutes=40))
    bump_generation(svc, 4)
    assert wd.handle_stalled_v2_jobs(stalled, svc) == 0
    assert table[0].status == "IN_PROGRESS"


@requires_redis
def test_stall_whose_job_commits_before_marking_is_left_alone(svc, watchdog):
    """The fail is fenced on the committed epoch the watchdog saw, so a
    job that commits an epoch after the stall was detected is not failed."""
    wd, table = watchdog
    table.append(_job(6))
    set_meta(svc, 6, committed_epoch=5)
    tracker = wd.V2StallTracker()
    wd.detect_stalled_v2_jobs(svc, tracker, now=T0)
    stalled = wd.detect_stalled_v2_jobs(svc, tracker, now=T0 + timedelta(minutes=40))
    assert [s[0] for s in stalled] == [6]
    svc.update_job_meta(6, "committed_epoch", 6)                                        # progress
    assert wd.handle_stalled_v2_jobs(stalled, svc) == 0
    assert table[0].status == "IN_PROGRESS" and svc.get_job_meta(6)["status"] == "IN_PROGRESS"


def test_log_action_only_logs(watchdog, monkeypatch):
    wd, table = watchdog
    table.append(_job(5))
    monkeypatch.setattr(wd, "V2_STALL_ACTION", "log")
    assert wd.handle_stalled_v2_jobs([(5, "no epoch committed", "0")]) == 0
    assert table[0].status == "IN_PROGRESS"


# --------------------------------------------------------------------------- #
# Terminal Redis states that PostgreSQL missed
# --------------------------------------------------------------------------- #

# A failure that reached Redis while PostgreSQL was down would leave the row
# IN_PROGRESS for good. The watchdog copies terminal Redis states onto it.


@requires_redis
def test_watchdog_reconciles_a_failure_postgres_missed(svc, watchdog):
    wd, table = watchdog
    table.extend([_job(7), _job(8)])
    set_meta(svc, 7, committed_epoch=5)
    set_meta(svc, 8, committed_epoch=5)
    assert svc.fail_job(7, "non-finite state", 0) >= 0      # Redis FAILED. The PostgreSQL write was lost
    assert wd.reconcile_terminal_v2_jobs(svc) == 1
    assert [j.status for j in table] == ["FAILED", "IN_PROGRESS"]   # job 8 is still running in Redis
    assert wd.reconcile_terminal_v2_jobs(svc) == 0                    # idempotent


@requires_redis
@pytest.mark.parametrize("terminal", ["FAILED", "COMPLETED"])
def test_reconciler_does_not_overwrite_a_recovery_after_its_redis_read(svc, monkeypatch, tmp_path, terminal):
    """The reconciler reads generation 0's terminal state,
    then a recovery installs generation 1 in Redis and sets the row
    IN_PROGRESS, then the reconciler writes. With real Redis and a real Peewee
    table the stale write must match nothing."""
    db = pw.SqliteDatabase(str(tmp_path / "jobs.db"))

    class Job(pw.Model):
        job_id = pw.IntegerField(primary_key=True)
        status = pw.CharField()
        job_type = pw.CharField()
        updated_at = pw.DateTimeField(null=True)
        completed_at = pw.DateTimeField(null=True)
        results_file_key = pw.CharField(default="")
        current_epoch = pw.IntegerField(default=0)

        class Meta:
            database = db
    db.create_tables([Job])
    clock = iter(T0 + timedelta(seconds=s) for s in range(1, 100))   # all after the row's T0
    wd = import_watchdog(monkeypatch, Job, db, utcnow=lambda: next(clock))
    try:
        Job.create(job_id=9, status="IN_PROGRESS", job_type="distributed_v2", updated_at=T0, current_epoch=5)
        set_meta(svc, 9, committed_epoch=5)
        if terminal == "FAILED":
            svc.fail_job(9, "lost projection", 0)
        else:
            svc.update_job_meta(9, "status", "COMPLETED")
            svc.update_job_meta(9, "results_file_key", "old-run.csv")
        reads, real = [], svc.get_job_meta

        def get_job_meta(job_id):
            meta = real(job_id)
            reads.append(meta)
            if len(reads) == 2:               # after the final observation: the recovery
                bump_generation(svc, 9)
                svc.update_job_meta(9, "status", "IN_PROGRESS")
                Job.update(status="IN_PROGRESS", current_epoch=3, updated_at=next(clock)).where(
                    Job.job_id == 9).execute()
            return meta
        monkeypatch.setattr(svc, "get_job_meta", get_job_meta)
        assert wd.reconcile_terminal_v2_jobs(svc) == 0
        row = Job.get_by_id(9)
        assert (row.status, row.current_epoch, row.results_file_key) == ("IN_PROGRESS", 3, "")
        # without a concurrent recovery the same missed projection is repaired
        monkeypatch.setattr(svc, "get_job_meta", real)
        svc.update_job_meta(9, "status", terminal)
        assert wd.reconcile_terminal_v2_jobs(svc) == 1
        assert Job.get_by_id(9).status == terminal
    finally:
        sys.modules.pop("watchdog", None)
        db.close()


# --------------------------------------------------------------------------- #
# Republication of monolithic jobs
# --------------------------------------------------------------------------- #

# Every republication path builds the message with MonolithicJobMessage.from_job,
# so a recovered adaptive job keeps its adaptive settings.


def _adaptive_job():
    return types.SimpleNamespace(
        job_id=7, input_bucket="input-files", input_file_key="k.csv", num_epochs=500, time_step=1e-3,
        snapshot_interval=50, gpu_enabled=True, force_algorithm="barnes_hut", barnes_hut_theta=0.7,
        softening_sq=1e-2, integration_method="rk4", adaptive_enabled=True, adaptive_eta=0.005,
        adaptive_dt_min=1e-6, adaptive_dt_max=0.01)


def test_message_from_job_carries_the_whole_configuration():
    msg = MonolithicJobMessage.from_job(_adaptive_job())
    assert (msg.adaptive_enabled, msg.adaptive_eta, msg.adaptive_dt_min, msg.adaptive_dt_max) == \
        (True, 0.005, 1e-6, 0.01)
    assert (msg.force_algorithm, msg.barnes_hut_theta, msg.softening_sq, msg.integration_method) == \
        ("barnes_hut", 0.7, 1e-2, "rk4")
    # every field but the schema version is taken from the job, not defaulted
    defaults = MonolithicJobMessage(job_id=0, bucket="", file_key="", num_of_epochs=0, time_step=0.0,
                                    snapshot_interval=0)
    for f in dataclasses.fields(MonolithicJobMessage):
        if f.name != "version":
            assert getattr(msg, f.name) != getattr(defaults, f.name), f.name
    assert MonolithicJobMessage.from_json(msg.to_json()) == msg


def test_republication_paths_use_the_single_builder():
    src = Path(__file__).resolve().parents[1] / "src"

    def direct_constructions(path):
        tree = ast.parse(path.read_text())
        return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "MonolithicJobMessage"]
    assert direct_constructions(src / "watchdog.py") == []
    # routes/jobs.py builds the message directly only at submission (from the
    # request). Resume and regeneration use from_job
    assert len(direct_constructions(src / "routes" / "jobs.py")) == 1
