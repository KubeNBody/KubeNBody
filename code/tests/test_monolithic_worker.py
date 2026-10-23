# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The monolithic worker (workers/monolithic_worker.py) and its base module
(worker_monolithic_base.py): leases and redelivered messages, the job thread
and the consumer, recovery accounting, the retry budget, finalization.

No database and no broker: `models` is stubbed and the channel is a fake.
"""
from __future__ import annotations

import ast
import threading
import time
import time as _t
import types
from datetime import datetime, timedelta

import peewee
import pika.exceptions
import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
    ReadTimeoutError,
)
from playhouse.pool import PooledSqliteDatabase
from psycopg2.extensions import QueryCanceledError

import job_timestamps
import rabbitmq
import schemas
import worker_metrics
import worker_monolithic_base as W
import workers.backends.gpu_backend as GB
import workers.monolithic_worker as MW
from constants import LEASE_DURATION_SECONDS, MAX_REPUBLISH_ATTEMPTS
from helpers import SRC, SymbolicColumn, patch_where_used, use_models
from transient_errors import is_transient_error
from worker_monolithic_base import JobPausedException
from workers.backends.cpu_backend import CPUBackend
from workers.base.compute_backend import ComputeError
from workers.monolithic_worker import _ConnectionThreadChannel
from workers.renew_loop import RenewLoop


# --------------------------------------------------------------------------- #
# A redelivered message vs an unexpired lease
# --------------------------------------------------------------------------- #

# A killed worker's redelivered message reaches its replacement within
# seconds, while the dead worker's lease is still unexpired. wait_out_dead_lease
# decides it: holder renews -> alive (drop the duplicate), lease expires
# unrenewed -> take over, instead of waiting for the watchdog's 30-min rule.


T0 = datetime(2026, 9, 29, 16, 0, 0)


class _Lease:
    """Fake job row + lease table driven by a scripted clock."""

    def __init__(self, monkeypatch, holder="dead-pod-1-CPU", renew_at=None, expires_at=None):
        self.B, self.t = W, T0
        self.row = types.SimpleNamespace(job_id=7, worker_id=holder, leased_at=T0 - timedelta(seconds=5))
        self.renew_at, self.expires_at, self.acquired = renew_at, expires_at, False
        monkeypatch.setattr(W, "Job", types.SimpleNamespace(job_id=None, get=lambda *_: self._row()))
        patch_where_used(monkeypatch, W, "acquire_job_lease", self._acquire)

    def _row(self):
        if self.renew_at is not None and self.t >= self.renew_at and not self.acquired:
            self.row.leased_at = self.renew_at            # the live holder renewed
        return types.SimpleNamespace(**vars(self.row))

    def _acquire(self, job_id, worker_id):
        if self.expires_at is not None and self.t >= self.expires_at:
            self.row.worker_id, self.row.leased_at, self.acquired = worker_id, self.t, True
        return self.acquired

    def sleep(self, s):
        self.t += timedelta(seconds=s)

    def run(self, should_stop=lambda: False):
        return self.B.wait_out_dead_lease(7, "new-pod-1-CPU", sleep=self.sleep, should_stop=should_stop,
                                          now=lambda: self.t)


def test_dead_holder_is_taken_over_when_its_lease_expires(monkeypatch):
    lease = _Lease(monkeypatch, expires_at=T0 + timedelta(seconds=LEASE_DURATION_SECONDS - 5))
    assert lease.run() == "acquired"
    waited = (lease.t - T0).total_seconds()
    assert LEASE_DURATION_SECONDS - 5 <= waited <= LEASE_DURATION_SECONDS + 15


def test_live_holder_renewal_drops_the_duplicate(monkeypatch):
    lease = _Lease(monkeypatch, renew_at=T0 + timedelta(seconds=100))
    assert lease.run() == "alive" and not lease.acquired
    assert (lease.t - T0).total_seconds() <= 120      # decided at the first poll after the renewal


def test_shutdown_while_waiting_requeues(monkeypatch):
    lease = _Lease(monkeypatch)
    calls = iter([False, False, True])
    assert lease.run(should_stop=lambda: next(calls)) == "stopped"


def test_gives_up_after_the_lease_duration(monkeypatch):
    lease = _Lease(monkeypatch)                       # never renewed, never acquirable
    assert lease.run() == "alive"
    assert (lease.t - T0).total_seconds() <= LEASE_DURATION_SECONDS + 60


def test_an_unreachable_database_while_waiting_raises_instead_of_dropping(monkeypatch):
    lease = _Lease(monkeypatch)

    def down(*a):
        raise peewee.OperationalError("server closed the connection unexpectedly")
    monkeypatch.setattr(lease.B, "Job", types.SimpleNamespace(job_id=None, get=down))
    with pytest.raises(peewee.OperationalError):
        lease.run()


def _deliver_to_a_leased_job(monkeypatch, redelivered, outcome):
    """Deliver a message for job 7, whose lease another worker holds, with
    wait_out_dead_lease answering `outcome`. Return the channel calls, the
    `sleep` each wait was given, and the channel's connection."""
    msg = types.SimpleNamespace(job_id=7, num_of_epochs=10, force_algorithm="direct",
                                integration_method="rk4")
    monkeypatch.setattr(schemas.MonolithicJobMessage, "from_json", staticmethod(lambda body: msg))
    monkeypatch.setattr(worker_metrics, "set_worker_status", lambda *a, **k: None)
    job = types.SimpleNamespace(job_id=7, status="IN_PROGRESS")
    use_models(monkeypatch, types.SimpleNamespace(
        Job=types.SimpleNamespace(job_id=None, get=lambda *a: job), utcnow=lambda: T0))
    monkeypatch.setattr(MW, "acquire_job_lease", lambda *a: False)
    waits = []

    def wait(job_id, worker_id, sleep, should_stop):
        waits.append(sleep)
        return outcome
    monkeypatch.setattr(MW, "wait_out_dead_lease", wait)
    w = MW.MonolithicWorker.__new__(MW.MonolithicWorker)
    w.compute = types.SimpleNamespace(name="cpu")
    w.worker_id = "new-pod-1-CPU"
    w.shutdown_handler = types.SimpleNamespace(current_job_id=None, check_shutdown=lambda: False)
    calls = []
    connection = types.SimpleNamespace(sleep=lambda s: None)
    ch = types.SimpleNamespace(connection=connection,
                               basic_ack=lambda **k: calls.append(("ack", k["delivery_tag"])),
                               basic_nack=lambda **k: calls.append(("nack", k["delivery_tag"], k["requeue"])),
                               stop_consuming=lambda: None)
    w._handle_message(ch, types.SimpleNamespace(delivery_tag=3, redelivered=redelivered), None, b"{}")
    return calls, waits, connection


def test_worker_waits_only_for_redelivered_messages(monkeypatch):
    # a fresh message for a leased job is a duplicate: dropped at once
    calls, waits, _ = _deliver_to_a_leased_job(monkeypatch, redelivered=False, outcome="alive")
    assert waits == [] and calls == [("ack", 3)]
    # a redelivered one waits out the lease, sleeping through the connection
    # so that the AMQP heartbeats keep flowing. A live holder: dropped
    calls, waits, conn = _deliver_to_a_leased_job(monkeypatch, redelivered=True, outcome="alive")
    assert waits == [conn.sleep] and calls == [("ack", 3)]
    # a shutdown during the wait hands the message back to the broker
    calls, waits, conn = _deliver_to_a_leased_job(monkeypatch, redelivered=True, outcome="stopped")
    assert waits == [conn.sleep] and calls == [("nack", 3, True)]


# --------------------------------------------------------------------------- #
# Jobs run in a thread. The connection thread keeps the heartbeats
# --------------------------------------------------------------------------- #

class _FakeConnection:
    def __init__(self):
        self.callbacks = []

    def add_callback_threadsafe(self, cb):
        self.callbacks.append(cb)

    def run_callbacks(self):
        while self.callbacks:
            self.callbacks.pop(0)()


class _FakeChannel:
    def __init__(self):
        self.connection = _FakeConnection()
        self.calls = []

    def basic_ack(self, delivery_tag):
        self.calls.append(("ack", delivery_tag))

    def basic_nack(self, delivery_tag, requeue=True):
        self.calls.append(("nack", delivery_tag, requeue))

    def stop_consuming(self):
        self.calls.append(("stop",))

    def basic_cancel(self, consumer_tag):
        self.calls.append(("cancel", consumer_tag))

    def basic_consume(self, queue, auto_ack, on_message_callback):
        self.calls.append(("consume", queue))
        return f"ctag-{len(self.calls)}"


def _worker(monkeypatch, backend_name):
    backend = types.SimpleNamespace(name=backend_name)
    monkeypatch.setattr(MW, "ShutdownHandler", lambda *a, **k: types.SimpleNamespace(
        current_job_id=None, check_shutdown=lambda: False))
    return MW, MW.MonolithicWorker(compute_backend=backend, worker_id="w-1")


def test_channel_calls_from_the_job_thread_run_on_the_connection_thread():
    ch = _FakeChannel()
    proxy = _ConnectionThreadChannel(ch)
    proxy.basic_nack(delivery_tag=7, requeue=True)
    proxy.basic_ack(delivery_tag=8)
    proxy.stop_consuming()
    assert ch.calls == []                         # nothing touched the channel yet
    ch.connection.run_callbacks()                 # the connection thread's I/O loop
    assert ch.calls == [("nack", 7, True), ("ack", 8), ("stop",)]
    assert proxy.connection.sleep is _t.sleep     # the lease wait does not drive I/O


def test_threaded_only_for_cpu(monkeypatch):
    assert _worker(monkeypatch, "cpu")[1]._threaded is True
    assert _worker(monkeypatch, "gpu")[1]._threaded is False       # pycuda.autoinit context


def test_on_message_returns_at_once_and_runs_the_job_in_a_thread(monkeypatch):
    MW, w = _worker(monkeypatch, "cpu")
    started, release, seen = threading.Event(), threading.Event(), {}

    def handle(ch, method, properties, body):
        seen["thread"] = threading.current_thread().name
        seen["proxy"] = type(ch).__name__
        started.set()
        release.wait(5)
        ch.basic_ack(delivery_tag=method.delivery_tag)

    w._handle_message = handle
    ch = _FakeChannel()
    w._on_message(ch, types.SimpleNamespace(delivery_tag=3, redelivered=False), None, b"{}")
    assert started.wait(5)                        # the callback returned while the job runs
    assert w.shutdown_handler.current_job_id == -1   # a signal now stops the job gracefully
    release.set(); w._job_thread.join(5)
    assert seen == {"thread": "monolithic-job", "proxy": "_JobChannel"}
    ch.connection.run_callbacks()
    assert ch.calls == [("ack", 3)]


def _consuming_worker(monkeypatch, backend="cpu", shutdown=lambda: False):
    MW, w = _worker(monkeypatch, backend)
    w.shutdown_handler = types.SimpleNamespace(current_job_id=None, check_shutdown=shutdown)
    ch = _FakeChannel()
    w._raw_channel, w._queue_name = ch, "monolithic_queue"
    w._thread_channel = MW._ConnectionThreadChannel(ch)     # as run()'s _connect sets it
    w._consume()
    return w, ch


def test_a_worker_takes_no_second_job_while_one_runs(monkeypatch):
    """With the message acknowledged at the claim, prefetch_count=1 does not
    hold the next delivery back, so a busy worker would run several jobs at
    once: the consumer is cancelled before the job starts and re-registered
    when it ends."""
    w, ch = _consuming_worker(monkeypatch)
    first_tag = w._consumer_tag
    running, release = threading.Event(), threading.Event()

    def handle(c, method, properties, body):
        running.set()
        release.wait(5)
    w._handle_message = handle
    w._on_message(ch, types.SimpleNamespace(delivery_tag=1, redelivered=False), None, b"{}")
    assert running.wait(5)
    assert ch.calls == [("consume", "monolithic_queue"), ("cancel", first_tag)]   # cancelled before the job
    assert w._consumer_tag is None
    release.set(); w._job_thread.join(5)
    ch.connection.run_callbacks()                 # _after_job on the connection thread
    assert ch.calls[-1] == ("consume", "monolithic_queue") and w._consumer_tag is not None
    assert not w._stop_loop


def test_a_worker_asked_to_stop_does_not_consume_again(monkeypatch):
    w, ch = _consuming_worker(monkeypatch, shutdown=lambda: True)
    w._handle_message = lambda *a: None
    w._on_message(ch, types.SimpleNamespace(delivery_tag=1, redelivered=False), None, b"{}")
    w._job_thread.join(5)
    ch.connection.run_callbacks()
    assert ("consume", "monolithic_queue") not in ch.calls[1:] and w._stop_loop


def test_the_synchronous_gpu_path_also_takes_one_job_at_a_time(monkeypatch):
    w, ch = _consuming_worker(monkeypatch, backend="gpu")
    seen = []
    w._handle_message = lambda c, m, p, b: seen.append(list(ch.calls))
    w._on_message(ch, types.SimpleNamespace(delivery_tag=1, redelivered=False), None, b"{}")
    assert seen[0][-1][0] == "cancel"             # cancelled while the job ran
    assert ch.calls[-1] == ("consume", "monolithic_queue")


def test_the_lease_renewer_never_holds_a_connection_between_renewals():
    """Behind pgbouncer (session mode) an open connection holds a server
    connection: idle renewer connections would exhaust the pool, later
    renewals time out and live jobs lose their leases. The renewer closes its
    connection for good after every renewal."""

    class DB:
        def __init__(self):
            self.closed, self.discarded = 0, 0

        def is_closed(self):
            return False

        def close(self):
            self.closed += 1

        def manual_close(self):
            self.discarded += 1
    db = DB()
    answers = iter([True, None, True])
    r = W.LeaseRenewer(1, "w", interval=0.01, renew=lambda *a: next(answers, True), db=db).start()
    time.sleep(0.1)
    r.stop()
    assert db.discarded >= 3 and db.closed == 0   # every renewal's connection is closed, none kept
    assert not r.lost                             # a failed renewal (None) is retried, not a loss


def test_a_failed_lease_renewal_is_retried_quickly_not_after_a_whole_interval():
    """After a database outage the renewal came a whole interval later, so the
    watchdog could find the lease lapsed first and republish a live job."""
    calls = []

    def renew(*a):
        calls.append(time.monotonic())
        return None if len(calls) < 3 else True      # the database is down twice
    db = types.SimpleNamespace(is_closed=lambda: True, manual_close=lambda: None)
    r = W.LeaseRenewer(1, "w", interval=0.2, renew=renew, db=db, retry_after_failure=0.02).start()
    time.sleep(0.5)
    r.stop()
    assert len(calls) >= 4
    assert calls[1] - calls[0] < 0.1 and calls[2] - calls[1] < 0.1    # retried fast while failing
    assert calls[3] - calls[2] >= 0.19                                # then back to the interval
    assert not r.lost


def test_renew_loop_retries_a_failed_renewal_quickly():
    calls = []

    class Loop(RenewLoop):
        RETRY_AFTER_FAILURE_S = 0.02

        def renew(self):
            calls.append(time.monotonic())
            if len(calls) < 3:
                raise ConnectionError("failing over")
            return True
    loop = Loop(0.2)
    loop.start()
    time.sleep(0.5)
    loop.stop()
    assert len(calls) >= 4
    assert calls[2] - calls[0] < 0.2                  # two quick retries
    assert calls[3] - calls[2] >= 0.19


def test_job_threads_return_their_database_connections(monkeypatch, tmp_path):
    """More sequential jobs than the pool holds, each querying the DB in
    its own thread (including one that fails), leave no connection checked out."""
    MW, w = _worker(monkeypatch, "cpu")
    pool = PooledSqliteDatabase(str(tmp_path / "pool.db"), max_connections=2, timeout=0.5,
                                check_same_thread=False)
    w._job_db = lambda: pool

    def handle(ch, method, properties, body):
        pool.execute_sql("select 1")
        if method.delivery_tag == 3:
            raise RuntimeError("job failed after touching the database")
        ch.basic_ack(delivery_tag=method.delivery_tag)

    w._handle_message = handle
    # the failing job's exception escapes its thread by design. Capture it so
    # that it is asserted here instead of surfacing as an unhandled-thread
    # warning (which CI can then treat as an error)
    escaped = []
    monkeypatch.setattr(threading, "excepthook", lambda args: escaped.append(args.exc_value))
    ch = _FakeChannel()
    for tag in range(1, 7):                       # 6 jobs > max_connections = 2
        w._on_message(ch, types.SimpleNamespace(delivery_tag=tag, redelivered=False), None, b"{}")
        w._job_thread.join(5)
        assert not w._job_thread.is_alive()
    assert [str(e) for e in escaped] == ["job failed after touching the database"]
    assert len(pool._in_use) == 0
    pool.execute_sql("select 1")                  # the pool still hands out connections
    pool.close()


# --------------------------------------------------------------------------- #
# recovery_count, and completed_at at the end of the integration
# --------------------------------------------------------------------------- #

# recovery_count is bumped when a lease resumes an earlier run and never
# reset. completed_at is the end of the integration, not of the snapshot
# combination.


class _Query:
    def where(self, *a, **k):
        return self

    def execute(self):
        return 1


def _fake_job_model():
    class FakeJob:
        job_id = SymbolicColumn("job_id")
        status = SymbolicColumn("status")
        recovery_count = SymbolicColumn("recovery_count")
        retry_count = SymbolicColumn("retry_count")
        updates: list = []

        class DoesNotExist(Exception):
            pass

        @classmethod
        def update(cls, **kw):
            cls.updates.append(kw)
            return _Query()
    return FakeJob


@pytest.mark.parametrize("redelivered,started_at,epoch,counted", [
    (False, None, 0, False),              # first delivery of a fresh job
    (True, None, 0, False),               # redelivered before any worker started it
    (True, datetime(2026, 1, 1), 0, True),    # previous holder died after loading the input
    (True, None, 4, True),                # previous holder died after a checkpoint
    (False, datetime(2026, 1, 1), 0, True),   # watchdog republish / takeover
    (False, None, 400, True),             # checkpointed progress
])
def test_record_recovery_if_resumed(monkeypatch, redelivered, started_at, epoch, counted):
    FakeJob = _fake_job_model()
    monkeypatch.setattr(W, "Job", FakeJob)
    job = types.SimpleNamespace(job_id=5, started_at=started_at, current_epoch=epoch)
    assert W.record_recovery_if_resumed(job, redelivered) is counted
    if counted:
        assert FakeJob.updates == [{"recovery_count": ("add", "recovery_count", 1)}]
    else:
        assert FakeJob.updates == []


def test_progress_and_finalize_never_touch_recovery_count():
    # no `recovery_count=0` keyword argument anywhere in the code (parsed, so
    # comments and docstrings do not count)
    for path in (SRC / "workers" / "monolithic_worker.py", SRC / "worker_monolithic_base.py"):
        resets = [n.lineno for n in ast.walk(ast.parse(path.read_text()))
                  if isinstance(n, ast.keyword) and n.arg == "recovery_count"
                  and isinstance(n.value, ast.Constant) and n.value.value == 0]
        assert resets == [], path


def _run_worker_message(monkeypatch, redelivered, job, process=None):
    clock = iter(datetime(2026, 9, 28, 12, 0, s) for s in range(50))
    events: list = []
    FakeJob = _fake_job_model()
    FakeJob.get = staticmethod(lambda *a, **k: job)
    use_models(monkeypatch, types.SimpleNamespace(Job=FakeJob, utcnow=lambda: next(clock)))
    msg = types.SimpleNamespace(
        job_id=job.job_id, num_of_epochs=10, force_algorithm="direct", integration_method="symplectic_euler",
        bucket="b", file_key="k", time_step=0.01, snapshot_interval=5, barnes_hut_theta=0.5,
        softening_sq=1e-4, gpu_enabled=False, adaptive_enabled=False, adaptive_eta=0.02,
        adaptive_dt_min=0.0, adaptive_dt_max=0.0)
    monkeypatch.setattr(schemas.MonolithicJobMessage, "from_json", staticmethod(lambda body: msg))
    monkeypatch.setattr(worker_metrics, "set_worker_status", lambda *a, **k: None)
    patch_where_used(monkeypatch, job_timestamps, "mark_job_started", lambda job_id: events.append("started"))
    monkeypatch.setattr(MW, "acquire_job_lease", lambda *a: True)
    monkeypatch.setattr(MW, "release_job_lease", lambda *a: events.append("released"))
    monkeypatch.setattr(MW, "load_bodies_for_job", lambda *a: ([{"n": 0}], 1))
    real_record = MW.record_recovery_if_resumed
    monkeypatch.setattr(MW, "record_recovery_if_resumed",
                        lambda j, r: events.append(("recovery", real_record(j, r))))

    monkeypatch.setattr(W, "Job", FakeJob)

    def fake_finalize(j, m, integration_completed_at=None):
        events.append(("finalize", integration_completed_at, next(clock)))
    monkeypatch.setattr(MW, "finalize_job", fake_finalize)

    w = MW.MonolithicWorker.__new__(MW.MonolithicWorker)
    w.compute = types.SimpleNamespace(name="cpu")
    w.worker_id = "pod-a-12-CPU"
    w.shutdown_handler = types.SimpleNamespace(current_job_id=None, check_shutdown=lambda: False)
    w._process_all_epochs = process or (lambda *a: events.append("integrated"))
    ch = types.SimpleNamespace(basic_ack=lambda **k: events.append("ack"),
                               basic_nack=lambda **k: events.append("nack"),
                               stop_consuming=lambda: None)
    w._handle_message(ch, types.SimpleNamespace(delivery_tag=1, redelivered=redelivered), None, b"{}")
    return events, FakeJob


def _job(**kw):
    base = dict(job_id=3, status="IN_PROGRESS", current_epoch=0, started_at=None)
    return types.SimpleNamespace(**{**base, **kw})


def test_worker_counts_a_redelivered_job_as_recovered(monkeypatch):
    events, FakeJob = _run_worker_message(monkeypatch, True, _job(current_epoch=4))
    assert ("recovery", True) in events
    assert {"recovery_count": ("add", "recovery_count", 1)} in FakeJob.updates
    # the message is acknowledged when the job is claimed, before it runs.
    # The lease is released at the end
    assert events.count("ack") == 1 and events.index("ack") < events.index("started")
    assert events[-1] == "released" and "nack" not in events


def test_worker_does_not_count_a_redelivery_that_never_started(monkeypatch):
    events, FakeJob = _run_worker_message(monkeypatch, True, _job())
    assert ("recovery", False) in events
    assert not any("recovery_count" in u for u in FakeJob.updates)


def test_worker_does_not_count_a_first_run(monkeypatch):
    events, FakeJob = _run_worker_message(monkeypatch, False, _job())
    assert ("recovery", False) in events
    assert not any("recovery_count" in u for u in FakeJob.updates)


def test_completed_at_is_taken_before_post_processing(monkeypatch):
    events, _ = _run_worker_message(monkeypatch, False, _job())
    assert events.index("started") < events.index("integrated")
    fin = next(e for e in events if isinstance(e, tuple) and e[0] == "finalize")
    _, integration_end, finalize_start = fin
    assert integration_end is not None and integration_end < finalize_start


def test_finalize_job_stores_the_integration_end(monkeypatch):
    FakeJob = _fake_job_model()
    monkeypatch.setattr(W, "Job", FakeJob)
    monkeypatch.setattr(W, "utcnow", lambda: datetime(2026, 9, 28, 13, 0))
    monkeypatch.setattr(W, "combine_epoch_snapshots", lambda **k: "combined.csv")
    end = datetime(2026, 9, 28, 12, 0)
    msg = types.SimpleNamespace(snapshot_interval=5, num_of_epochs=10)
    W.finalize_job(types.SimpleNamespace(job_id=4), msg, integration_completed_at=end)
    (upd,) = FakeJob.updates
    assert upd["completed_at"] == end and upd["status"] == "COMPLETED"
    assert "recovery_count" not in upd
    FakeJob.updates.clear()
    W.finalize_job(types.SimpleNamespace(job_id=4), msg)     # finalize-only path
    assert FakeJob.updates[0]["completed_at"] == datetime(2026, 9, 28, 13, 0)


def test_a_finalize_error_leaves_the_job_to_the_retry_budget(monkeypatch):
    """A transient storage error while combining snapshots leaves the finished
    job alone (marked FAILED, the retry that follows would drop it). The
    caller's budget decides."""
    FakeJob = _fake_job_model()
    monkeypatch.setattr(W, "Job", FakeJob)

    def combine(**k):
        raise ConnectionError("S3 unavailable")
    monkeypatch.setattr(W, "combine_epoch_snapshots", combine)
    msg = types.SimpleNamespace(snapshot_interval=5, num_of_epochs=10)
    with pytest.raises(ConnectionError):
        W.finalize_job(types.SimpleNamespace(job_id=4), msg)
    assert FakeJob.updates == []                         # not FAILED, not touched

# --------------------------------------------------------------------------- #
# The finalize-only shortcut
# --------------------------------------------------------------------------- #

# The worker trusts the epoch pointer only when the final snapshot is really
# in object storage.


def test_final_epoch_with_its_snapshot_is_finalized_only(monkeypatch):
    monkeypatch.setattr(MW, "final_snapshot_is_durable", lambda job_id, n: True)
    events, _ = _run_worker_message(monkeypatch, True, _job(current_epoch=10))
    assert "integrated" not in events
    assert any(isinstance(e, tuple) and e[0] == "finalize" for e in events)
    assert events.index("ack") < events.index(next(e for e in events if isinstance(e, tuple) and e[0] == "finalize"))
    assert events[-1] == "released"


def test_final_epoch_without_its_snapshot_is_recomputed(monkeypatch):
    monkeypatch.setattr(MW, "final_snapshot_is_durable", lambda job_id, n: False)
    job = _job(current_epoch=10)
    events, FakeJob = _run_worker_message(monkeypatch, True, job)
    assert job.current_epoch == 9                       # resumes from the checkpoint before the end
    assert any(u.get("current_epoch") == 9 for u in FakeJob.updates)
    assert events.index("integrated") < next(i for i, e in enumerate(events)
                                             if isinstance(e, tuple) and e[0] == "finalize")
    assert events.index("ack") < events.index("integrated") and events[-1] == "released"


# --------------------------------------------------------------------------- #
# The retry budget, driven through the real handler and epoch loop
# --------------------------------------------------------------------------- #

# A final snapshot upload that keeps failing must exhaust the retry budget:
# the budget is not reset by computing the final epoch, only once its upload
# is durable. CPU backend, four bodies.


def _stateful_job_model(state):
    class Q:
        def __init__(self, kw=None):
            self.kw = kw

        def where(self, *a, **k):
            return self

        def execute(self):
            for k, v in self.kw.items():
                state[k] = state[k] + v[2] if isinstance(v, tuple) and v[0] == "add" else v
            return 1

        def scalar(self):
            return state["retry_count"]

    class FakeJob:
        job_id = SymbolicColumn("job_id")
        status = SymbolicColumn("status")
        retry_count = SymbolicColumn("retry_count")
        recovery_count = SymbolicColumn("recovery_count")
        worker_id = SymbolicColumn("worker_id")
        leased_at = SymbolicColumn("leased_at")
        updates: list = []

        class DoesNotExist(Exception):
            pass

        @classmethod
        def update(cls, **kw):
            cls.updates.append(kw)
            return Q(kw)

        @staticmethod
        def select(*a):
            return Q()

        @staticmethod
        def get(*a, **k):
            return types.SimpleNamespace(**state)
    return FakeJob


def _deliver_with_real_loop(monkeypatch, state, upload_ok, cancel_after_progress=False,
                            upload_error=None, fail_at=None):
    """fail_at: (stage, error) raises error at 'claim' (lease acquisition,
    before the acknowledgement), 'resume' (loading the bodies) or 'finalize'."""
    events: list = []
    FakeJob = _stateful_job_model(state)
    if cancel_after_progress:
        # the API cancels the job once it has made progress. Rows behave like
        # Peewee models, whose save() writes every field of the loaded row
        def get(*a, **k):
            if state["current_epoch"] > 0:
                state["status"] = "CANCELLED"
            row = types.SimpleNamespace(**state)
            row.save = lambda: state.update({f: v for f, v in vars(row).items() if f in state})
            return row
        FakeJob.get = staticmethod(get)
    use_models(monkeypatch, types.SimpleNamespace(Job=FakeJob, utcnow=lambda: datetime(2026, 10, 1)))
    monkeypatch.setattr(W, "Job", FakeJob)
    monkeypatch.setattr(W, "utcnow", lambda: datetime(2026, 10, 1))
    msg = types.SimpleNamespace(
        job_id=state["job_id"], num_of_epochs=10, force_algorithm="direct",
        integration_method="symplectic_euler", bucket="b", file_key="k", time_step=1e-3,
        snapshot_interval=0, barnes_hut_theta=0.5, softening_sq=1e-2, gpu_enabled=False,
        adaptive_enabled=False, adaptive_eta=0.02, adaptive_dt_min=0.0, adaptive_dt_max=0.0)
    monkeypatch.setattr(schemas.MonolithicJobMessage, "from_json", staticmethod(lambda body: msg))
    for name in ("set_worker_status", "observe_epoch_batch", "observe_monolithic_job"):
        monkeypatch.setattr(worker_metrics, name, lambda *a, **k: None)
    patch_where_used(monkeypatch, job_timestamps, "mark_job_started", lambda job_id: None)
    stage, error = fail_at or (None, None)

    def fail(name, result=None):
        def step(*a, **k):
            if stage == name:
                raise error
            if name == "finalize":
                events.append(name)
            return result
        return step
    monkeypatch.setattr(MW, "acquire_job_lease", fail("claim", True))
    monkeypatch.setattr(MW, "renew_job_lease", lambda *a: True)
    monkeypatch.setattr(MW, "release_job_lease", lambda *a: None)
    monkeypatch.setattr(MW, "record_recovery_if_resumed", lambda j, r: None)
    monkeypatch.setattr(MW, "final_snapshot_is_durable", lambda job_id, n: False)
    monkeypatch.setattr(MW, "upload_snapshot_from_arrays", lambda *a, **k: events.append("upload"))
    bodies = [dict(idBody=i + 1, n=i, x=float(i), y=0.0, z=0.0, vx=0.0, vy=0.1 * i, vz=0.0, m=1.0)
              for i in range(4)]
    def load(*a):
        fail("resume")()
        return [dict(b) for b in bodies], 1
    monkeypatch.setattr(MW, "load_bodies_for_job", load)

    def drain():
        events.append("drain")
        if not upload_ok:
            raise upload_error or RuntimeError("S3 PUT failed: volume full")
    patch_where_used(monkeypatch, W, "get_snapshot_uploader", lambda: types.SimpleNamespace(drain=drain))
    monkeypatch.setattr(MW, "finalize_job", fail("finalize"))

    def publish(**k):
        events.append(("publish", k["routing_key"]))
        if k["properties"].expiration is not None:
            events.append(("ttl", k["properties"].expiration))

    w = MW.MonolithicWorker.__new__(MW.MonolithicWorker)
    w.compute = CPUBackend()
    w.worker_id = "pod-a-12-CPU"
    w.shutdown_handler = types.SimpleNamespace(current_job_id=None, check_shutdown=lambda: False)
    w._queue_name = "monolithic_queue"
    ch = types.SimpleNamespace(basic_ack=lambda **k: events.append("ack"),
                               basic_nack=lambda **k: events.append(("nack", k.get("requeue"))),
                               basic_publish=publish, stop_consuming=lambda: None)
    w._handle_message(ch, types.SimpleNamespace(delivery_tag=1, redelivered=True), None, b"{}")
    return events


def _monolithic_state():
    return dict(job_id=3, status="IN_PROGRESS", current_epoch=0, started_at=None, retry_count=0,
                recovery_count=0, current_simulation_time=0.0,
                # what a republished message is built from (MonolithicJobMessage.from_job)
                input_bucket="b", input_file_key="k", num_epochs=10, time_step=1e-3, snapshot_interval=0,
                gpu_enabled=False, force_algorithm="direct", barnes_hut_theta=0.5, softening_sq=1e-2,
                integration_method="symplectic_euler", adaptive_enabled=False, adaptive_eta=0.02,
                adaptive_dt_min=0.0, adaptive_dt_max=0.0, leased_at=None)


def test_persistent_final_upload_failure_exhausts_the_retry_budget(monkeypatch):
    state = _monolithic_state()
    deliveries = 0
    while state["status"] == "IN_PROGRESS" and deliveries < 2 * MAX_REPUBLISH_ATTEMPTS + 2:
        events = _deliver_with_real_loop(monkeypatch, state, upload_ok=False)
        deliveries += 1
        # acknowledged at the claim. The failed job is handed back (expired
        # lease + republish) instead of nacked, never finalized
        assert events[0] == "ack" and "drain" in events and "finalize" not in events
        assert not any(isinstance(e, tuple) and e[0] == "nack" for e in events)
    assert state["status"] == "FAILED"
    assert deliveries == MAX_REPUBLISH_ATTEMPTS + 1
    assert not any(isinstance(e, tuple) and e[0] == "publish" for e in events)   # dropped, not republished


def test_transient_final_upload_failure_recovers_and_resets_the_budget(monkeypatch):
    state = _monolithic_state()
    first = _deliver_with_real_loop(monkeypatch, state, upload_ok=False)
    assert first[-1] == ("publish", "monolithic_queue") and state["retry_count"] == 1
    assert state["leased_at"] is not None              # handed back with an expired lease
    second = _deliver_with_real_loop(monkeypatch, state, upload_ok=True)
    assert second[0] == "ack" and second[-1] == "finalize"
    assert state["retry_count"] == 0 and state["current_epoch"] == 10


# An outage of the object store or the database is not the job's fault: the
# job is handed back after a delay (through its queue's retry queue, a
# per-message TTL) without spending the retry budget, so an outage of minutes
# does not fail a healthy job. A permanent error (above) still counts.

def _s3_down():
    return EndpointConnectionError(endpoint_url="http://minio:9000")


def _s3_503():
    return ClientError({"Error": {"Code": "ServiceUnavailable"}}, "GetObject")


def _s3_missing():
    return ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")


def _db_down():
    return peewee.OperationalError("could not connect to server")


@pytest.mark.parametrize("kw", [
    dict(upload_ok=False, upload_error=_s3_down()),                  # final snapshot drain
    dict(upload_ok=True, fail_at=("resume", _s3_503())),              # checkpoint load
    dict(upload_ok=True, fail_at=("finalize", _s3_down())),           # snapshot combine
    dict(upload_ok=True, fail_at=("finalize", _db_down())),
], ids=["drain", "resume", "finalize", "finalize-db"])
def test_an_outage_hands_the_job_back_later_without_spending_the_budget(monkeypatch, kw):
    state = _monolithic_state()
    events = _deliver_with_real_loop(monkeypatch, state, **kw)
    assert events[0] == "ack" and not any(isinstance(e, tuple) and e[0] == "nack" for e in events)
    assert events[-2:] == [("publish", "monolithic_queue.retry"),
                           ("ttl", str(MW.TRANSIENT_HANDBACK_DELAY_MS))]
    assert state["retry_count"] == 0 and state["status"] == "IN_PROGRESS"
    # expired (claimable) but within the watchdog's grace while the message waits
    assert state["leased_at"] == datetime(2026, 10, 1) - timedelta(
        seconds=W.LEASE_DURATION_SECONDS + W.DELAYED_HANDBACK_MARGIN_S)


def test_an_outage_longer_than_the_budget_does_not_fail_the_job(monkeypatch):
    state = _monolithic_state()
    for _ in range(3 * MAX_REPUBLISH_ATTEMPTS + 2):
        events = _deliver_with_real_loop(monkeypatch, state, upload_ok=False, upload_error=_s3_down())
        assert ("publish", "monolithic_queue.retry") in events
        assert state["status"] == "IN_PROGRESS" and state["retry_count"] == 0
    events = _deliver_with_real_loop(monkeypatch, state, upload_ok=True)     # storage is back
    assert events[-1] == "finalize" and state["current_epoch"] == 10


def test_a_permanent_error_after_an_outage_still_counts(monkeypatch):
    state = _monolithic_state()
    _deliver_with_real_loop(monkeypatch, state, upload_ok=False, upload_error=_s3_down())
    events = _deliver_with_real_loop(monkeypatch, state, upload_ok=False)    # RuntimeError
    assert state["retry_count"] == 1 and events[-1] == ("publish", "monolithic_queue")


def test_an_outage_before_the_claim_is_delayed_not_requeued_at_once(monkeypatch):
    """Before the claim the delivery is still held: a nack-requeue would put
    it straight back, a hot loop for the outage's length."""
    state = _monolithic_state()
    events = _deliver_with_real_loop(monkeypatch, state, upload_ok=True, fail_at=("claim", _db_down()))
    assert events == [("publish", "monolithic_queue.retry"), ("ttl", str(rabbitmq.RETRY_DELAY_MS)), "ack"]
    assert state["retry_count"] == 0


def _no_credentials():
    return NoCredentialsError()


def _bad_param():
    return ParamValidationError(report="Invalid bucket name")


def _read_timeout():
    return ReadTimeoutError(endpoint_url="http://minio:9000")


def _db_uninitialised():
    return peewee.InterfaceError("Error, database must be initialized before opening a connection.")


def _statement_timeout():
    try:
        try:
            raise QueryCanceledError("canceling statement due to statement timeout")
        except QueryCanceledError as e:
            raise peewee.OperationalError(*e.args)     # as peewee re-raises it
    except peewee.OperationalError as wrapped:
        return wrapped


@pytest.mark.parametrize("exc,transient", [
    (_s3_down(), True), (_s3_503(), True), (_db_down(), True), (_read_timeout(), True),
    (_s3_missing(), False), (ValueError("corrupt checkpoint"), False),
    # configuration faults and statement timeouts repeat on every attempt:
    # counted, so the job fails instead of retrying forever
    (_no_credentials(), False), (_bad_param(), False),
    (_db_uninitialised(), False), (_statement_timeout(), False),
])
def test_transient_error_classification(exc, transient):
    assert is_transient_error(exc) is transient


def test_the_worker_declares_its_retry_queue_as_the_api_does():
    declared = {}
    ch = types.SimpleNamespace(queue_declare=lambda queue, durable, arguments=None: declared.update(
        {queue: arguments}))
    rabbitmq.ensure_rabbitmq_queues(ch)
    expected = dict(declared)
    declared.clear()
    for q in (rabbitmq.MONOLITHIC_QUEUE, rabbitmq.GPU_MONOLITHIC_QUEUE):
        rabbitmq.declare_retry_queue(ch, q)
        assert declared[q + ".retry"] == expected[q + ".retry"] == {
            "x-dead-letter-exchange": "", "x-dead-letter-routing-key": q}


def test_cancel_keeps_the_progress_written_during_the_run(monkeypatch):
    """Cancelling must not job.save() the row loaded at the claim: that would
    put back its epoch and counters over the progress written since with
    Job.update."""
    state = _monolithic_state()
    state.update(retry_count=1, recovery_count=2)
    events = _deliver_with_real_loop(monkeypatch, state, upload_ok=True, cancel_after_progress=True)
    assert state["status"] == "CANCELLED" and "finalize" not in events
    assert state["current_epoch"] == 10 and state["current_simulation_time"] > 0
    assert state["recovery_count"] == 2


# --------------------------------------------------------------------------- #
# Snapshot metadata of adaptive jobs
# --------------------------------------------------------------------------- #

# download_file returns decoded text. A valid sidecar must parse.


def test_adaptive_snapshot_metadata_is_parsed(monkeypatch):
    monkeypatch.setattr(W, "download_file",
                        lambda bucket, key: '{"simulation_time": 0.15, "dt_used": 0.005}')
    assert W.load_snapshot_metadata(3, 10) == {"simulation_time": 0.15, "dt_used": 0.005}

    def missing(bucket, key):
        raise FileNotFoundError(key)
    monkeypatch.setattr(W, "download_file", missing)
    assert W.load_snapshot_metadata(3, 10) is None


def test_a_message_that_cannot_be_parsed_is_dropped(monkeypatch):
    """No redelivery can parse a malformed message: requeueing it looped
    forever, since the retry budget needs a job id to count against."""
    FakeJob = _fake_job_model()
    use_models(monkeypatch, types.SimpleNamespace(Job=FakeJob, utcnow=lambda: datetime(2026, 10, 9)))
    monkeypatch.setattr(worker_metrics, "set_worker_status", lambda *a, **k: None)
    w = MW.MonolithicWorker.__new__(MW.MonolithicWorker)
    w.compute = types.SimpleNamespace(name="cpu")
    w.worker_id = "pod-a-12-CPU"
    w.shutdown_handler = types.SimpleNamespace(current_job_id=None, check_shutdown=lambda: False)
    nacks = []
    ch = types.SimpleNamespace(basic_ack=lambda **k: None, stop_consuming=lambda: None,
                               basic_nack=lambda **k: nacks.append(k.get("requeue")))
    w._handle_message(ch, types.SimpleNamespace(delivery_tag=1, redelivered=False), None, b"not json")
    assert nacks == [False]


# --------------------------------------------------------------------------- #
# A lost broker connection does not kill the job
# --------------------------------------------------------------------------- #

# The job's message is acknowledged at the claim, so the job needs the broker
# only to hand itself back or take the next job. run() replaces a lost
# connection while the job thread keeps computing.


class _BrokerConnection:
    """A pika BlockingConnection as run() uses it. on_poll(conn) is called on
    every process_data_events."""

    def __init__(self, on_poll):
        self._closed_error = pika.exceptions.ConnectionWrongStateError
        self.is_open, self.callbacks, self.on_poll = True, [], on_poll
        self.ch = _FakeChannel()
        self.ch.connection = self
        self.ch.queue_declare = lambda **k: None
        self.ch.basic_qos = lambda **k: None

    def channel(self):
        return self.ch

    def add_callback_threadsafe(self, cb):
        if not self.is_open:                      # as pika does
            raise self._closed_error("Connection is closed")
        self.callbacks.append(cb)

    def process_data_events(self, time_limit=0):
        while self.callbacks:
            self.callbacks.pop(0)()
        self.on_poll(self)
        time.sleep(0.005)

    def close(self):
        if not self.is_open:
            raise self._closed_error("Connection is closed")
        self.is_open = False


def test_a_lost_broker_connection_is_replaced_while_the_job_runs(monkeypatch):
    MW, w = _worker(monkeypatch, "cpu")
    cleaned = []
    w.compute = types.SimpleNamespace(name="cpu", warmup=lambda: None, cleanup=lambda: cleaned.append(1))
    running, release = threading.Event(), threading.Event()

    def handle(ch, method, properties, body):
        running.set()
        release.wait(5)
        ch.basic_ack(delivery_tag=method.delivery_tag)    # its connection is gone: dropped, no raise
    w._handle_message = handle
    polls = []

    def first(conn):
        polls.append(1)
        if len(polls) == 1:
            w._on_message(conn.ch, types.SimpleNamespace(delivery_tag=1, redelivered=False), None, b"{}")
        elif running.is_set():
            conn.is_open = False
            raise pika.exceptions.StreamLostError("Transport indicated EOF")

    def second(conn):
        polls.append(2)
        release.set()
        done = ("consume", "q") in conn.ch.calls and not w._job_thread.is_alive()
        if done or len(polls) > 2000:
            w._stop_loop = True
    conns = [_BrokerConnection(first), _BrokerConnection(second)]
    made = iter(conns)
    patch_where_used(monkeypatch, rabbitmq, "get_rabbitmq_connection", lambda: next(made))
    w.run("q")
    old, new = conns[0].ch.calls, conns[1].ch.calls
    assert old == [("consume", "q"), ("cancel", "ctag-1")]   # the job's ack never reached a dead channel
    assert new == [("consume", "q")]              # consuming again on the new connection once the job ended
    assert not w._job_thread.is_alive() and len(polls) < 2000
    assert cleaned == [1]                         # cleanup ran. Closing the dead connection did not raise


def test_an_unreachable_broker_while_idle_ends_the_worker(monkeypatch):
    MW, w = _worker(monkeypatch, "cpu")
    cleaned = []
    w.compute = types.SimpleNamespace(name="cpu", warmup=lambda: None, cleanup=lambda: cleaned.append(1))

    def down():
        raise Exception("Could not connect to RabbitMQ")
    patch_where_used(monkeypatch, rabbitmq, "get_rabbitmq_connection", down)
    with pytest.raises(Exception, match="Could not connect"):
        w.run("q")
    assert cleaned == [1]


def test_the_synchronous_gpu_path_services_heartbeats_between_epochs(monkeypatch):
    """The GPU job runs inside the consumer callback: the epoch loop services
    the connection (at most once a second). A lost connection there does not
    stop the job."""
    MW, w = _worker(monkeypatch, "gpu")
    serviced = []
    w._channel = types.SimpleNamespace(connection=types.SimpleNamespace(
        process_data_events=lambda time_limit: serviced.append(time_limit)))
    w._service_heartbeats()
    w._service_heartbeats()                       # within the second: skipped
    assert serviced == [0]

    def lost(time_limit):
        raise pika.exceptions.StreamLostError("EOF")
    w._channel = types.SimpleNamespace(connection=types.SimpleNamespace(process_data_events=lost))
    w._last_heartbeat_service = 0.0
    w._service_heartbeats()                       # logged, not raised
    assert w._channel is None
    # the threaded (CPU) path leaves the connection to the connection thread
    MW, cpu = _worker(monkeypatch, "cpu")
    cpu._channel = types.SimpleNamespace(connection=types.SimpleNamespace(
        process_data_events=lambda time_limit: serviced.append("cpu")))
    cpu._service_heartbeats()
    assert serviced == [0]


# --------------------------------------------------------------------------- #
# A job's snapshot uploads end with it
# --------------------------------------------------------------------------- #

def test_a_paused_job_settles_its_uploads(monkeypatch):
    """The uploader is shared across jobs: a job that ends without drain()
    must not leave a failed upload's error to fail the next job."""
    settled = []
    monkeypatch.setattr(MW, "settle_snapshot_uploads", lambda: settled.append(1))

    def paused(*a):
        raise JobPausedException("paused at epoch 5")
    events, _ = _run_worker_message(monkeypatch, False, _job(), process=paused)
    assert settled == [1] and "finalize" not in str(events)


def test_a_finalize_suppressed_by_a_cancel_is_not_counted_as_completed(monkeypatch):
    FakeJob = _fake_job_model()
    monkeypatch.setattr(_Query, "execute", lambda self: 0)    # status was CANCELLED: no row updated
    monkeypatch.setattr(W, "Job", FakeJob)
    completed = []
    monkeypatch.setattr(W, "_record_completion", lambda *a: completed.append(a))
    W.finalize_job(types.SimpleNamespace(job_id=4), types.SimpleNamespace(snapshot_interval=0, num_of_epochs=10))
    assert completed == []


@pytest.mark.parametrize("input_file_id,rows,expected", [(5, {5: 20001}, 20001), (None, {}, 0), (6, {}, 0)])
def test_a_completed_job_counts_its_input_files_bodies(monkeypatch, input_file_id, rows, expected):
    class InputFile:
        file_id = SymbolicColumn("file_id")

        @staticmethod
        def get_or_none(cond):
            n = rows.get(cond[2])
            return types.SimpleNamespace(num_bodies=n) if n is not None else None
    monkeypatch.setattr(W, "InputFile", InputFile)
    recorded = []
    monkeypatch.setattr(W.prometheus_metrics, "record_job_completed", lambda b, e: recorded.append((b, e)))
    W._record_completion(types.SimpleNamespace(input_file_id=input_file_id), 10)
    assert recorded == [(expected, 10)]


def test_final_snapshot_check_raises_on_an_outage_instead_of_rewinding(monkeypatch):
    def head(exc):
        def _head(**_):
            if exc is not None:
                raise exc
        return types.SimpleNamespace(meta=types.SimpleNamespace(
            client=types.SimpleNamespace(head_object=_head)))

    monkeypatch.setattr(W, "get_s3", lambda: head(None))
    assert W.final_snapshot_is_durable(1, 10) is True
    monkeypatch.setattr(W, "get_s3", lambda: head(_s3_missing()))
    assert W.final_snapshot_is_durable(1, 10) is False
    monkeypatch.setattr(W, "get_s3", lambda: head(_s3_down()))
    with pytest.raises(Exception):
        W.final_snapshot_is_durable(1, 10)


# A CUDA fault leaves the device unusable: the job goes back through the
# counted retry path, and the worker exits for its pod to restart it
def test_a_compute_error_that_breaks_the_device_hands_the_job_back_and_restarts(monkeypatch):
    failures, exits = [], []
    monkeypatch.setattr(MW.MonolithicWorker, "_on_failure",
                        lambda self, ch, method, props, body, message, job, acked, e: failures.append(e))
    monkeypatch.setattr(MW, "exit_for_restart", lambda reason: exits.append(reason))

    def fault(*a):
        raise ComputeError("gpu", "an illegal memory access was encountered", restart_worker=True)
    _run_worker_message(monkeypatch, False, _job(), process=fault)
    assert len(failures) == 1 and failures[0].restart_worker and failures[0].recoverable
    assert exits == [str(failures[0])]


def test_only_cuda_driver_errors_restart_the_worker(monkeypatch):
    class FakeCudaError(Exception):
        pass
    monkeypatch.setattr(GB, "CudaError", FakeCudaError)
    cuda = GB._compute_error("gpu", "GPU computation failed", FakeCudaError("launch failed"))
    other = GB._compute_error("gpu", "GPU computation failed", ValueError("bad input"))
    assert cuda.restart_worker and cuda.recoverable
    assert not other.restart_worker and not other.recoverable
