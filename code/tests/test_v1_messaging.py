# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""v1 (RabbitMQ) messaging: the worker backend waits for tasks instead of
polling, reconnects after a lost connection, counts failed deliveries and
retries them after a delay. The merger and orchestrator retry the same way
and fail the job past the cap. Fake pika connections, no broker."""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import types

import peewee
import pika
import pika.exceptions
import pika.spec
import psycopg2
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

import file_service
import rabbitmq
import v1_epochs
from helpers import patch_where_used
from workers import distributed_worker as DW
from workers.backends import rabbitmq_backend as RB
from workers.distributed_worker import DistributedWorker


TASK = {"job_id": 7, "bucket": "b", "file_key": "k", "epoch_num": 2, "package_num": 1,
        "package_id": 70, "package_size": 4, "num_of_epochs": 3}


class FakeChannel:
    def __init__(self, connection):
        self.connection = connection
        self.is_open = True
        self.acks, self.nacks, self.published, self.declared = [], [], [], []
        self.callback = None
        self.confirming = False

    def confirm_delivery(self):
        self.confirming = True

    def basic_qos(self, **k):
        pass

    def basic_consume(self, queue, on_message_callback, auto_ack):
        self.callback = on_message_callback
        return "ctag"

    def basic_ack(self, delivery_tag):
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag, requeue):
        self.nacks.append((delivery_tag, requeue))

    def basic_publish(self, exchange, routing_key, body, properties, mandatory=False):
        self.published.append((routing_key, body, properties))

    def queue_declare(self, queue, durable, arguments=None):
        self.declared.append((queue, arguments))

    def close(self):
        self.is_open = False


class FakeConnection:
    """Delivers one queued (body, headers) per process_data_events call."""

    def __init__(self, deliveries=()):
        self.is_open = True
        self.pending = list(deliveries)
        self.waits = []
        self.tag = 0
        self.ch = FakeChannel(self)

    def channel(self):
        return self.ch

    def process_data_events(self, time_limit):
        self.waits.append(time_limit)
        if not self.is_open:
            raise pika.exceptions.StreamLostError("lost")
        if self.pending:
            body, headers = self.pending.pop(0)
            self.tag += 1
            self.ch.callback(self.ch, pika.spec.Basic.Deliver(delivery_tag=self.tag, routing_key="new"),
                             pika.BasicProperties(headers=headers), body)

    def close(self):
        self.is_open = False


@pytest.fixture
def backend(monkeypatch):
    """A RabbitMQBackend whose connect() takes the next FakeConnection of
    `backend.connections`."""
    b = RB.RabbitMQBackend()
    b.connections = []
    monkeypatch.setattr(RB, "get_rabbitmq_connection", lambda max_retries=None: b.connections.pop(0))
    monkeypatch.setattr(RB, "ensure_rabbitmq_queues", lambda ch: None)
    return b


def _body(**kw):
    return json.dumps({**TASK, **kw}).encode()


# --------------------------------------------------------------------------- #
# RabbitMQBackend
# --------------------------------------------------------------------------- #

# An empty queue waits up to block_ms (servicing heartbeats) instead of
# returning at once, which made idle workers poll the broker in a hot loop.
def test_an_empty_claim_waits_for_block_ms(backend):
    conn = FakeConnection()
    backend.connections = [conn]
    backend.connect()
    assert backend.claim_task("w", "new", block_ms=1000) is None
    assert conn.waits == [1.0]
    conn.pending.append((_body(), None))
    task = backend.claim_task("w", "new", block_ms=1000)
    assert task.job_id == 7 and task.raw_data["__deliveries__"] == 1


# An unparsable message is dropped, not left unacknowledged on the channel.
def test_an_unparsable_message_is_rejected_without_requeue(backend):
    conn = FakeConnection([(b"not json", None)])
    backend.connections = [conn]
    backend.connect()
    assert backend.claim_task("w", "new", block_ms=10) is None
    assert conn.ch.nacks == [(1, False)]


# A failed task goes to the retry queue (dead-lettered back after the delay)
# with its failed deliveries counted, which the worker's delivery cap reads.
def test_a_nacked_task_is_retried_later_with_its_delivery_count(backend):
    conn = FakeConnection([(_body(), {rabbitmq.DELIVERIES_HEADER: 2})])
    backend.connections = [conn]
    backend.connect()
    task = backend.claim_task("w", "new", block_ms=10)
    assert task.raw_data["__deliveries__"] == 3
    backend.nack_task(task, requeue=True)
    (queue, body, props), = conn.ch.published
    assert queue == "new.retry" and body == _body()
    assert props.headers[rabbitmq.DELIVERIES_HEADER] == 3 and props.expiration == str(rabbitmq.RETRY_DELAY_MS)
    assert conn.ch.acks == [1] and conn.ch.nacks == []


# A delivery tag of a lost channel is not acked on the new one, where it
# would name another message (the broker redelivers the old one).
def test_a_tag_of_a_lost_connection_is_not_acked_on_the_new_one(backend):
    old, new = FakeConnection([(_body(), None)]), FakeConnection([(_body(), None)])
    backend.connections = [old, new]
    backend.connect()
    stale = backend.claim_task("w", "new", block_ms=10)
    old.is_open = False
    backend.disconnect()
    backend.connect()
    fresh = backend.claim_task("w", "new", block_ms=10)
    backend.ack_task(stale)
    backend.nack_task(stale)
    assert new.ch.acks == [] and new.ch.published == []
    backend.ack_task(fresh)
    assert new.ch.acks == [1]


def test_retry_queues_dead_letter_back_to_their_queue():
    ch = FakeChannel(None)
    rabbitmq.ensure_rabbitmq_queues(ch)
    args = dict(ch.declared)
    assert args["new"] is None
    for q in rabbitmq.RETRIED_QUEUES:
        assert args[q + ".retry"] == {"x-dead-letter-exchange": "", "x-dead-letter-routing-key": q}


# --------------------------------------------------------------------------- #
# Worker loop
# --------------------------------------------------------------------------- #

def _worker(backend, monkeypatch, processed):
    w = DistributedWorker(types.SimpleNamespace(name="cpu"), backend, worker_name="w")
    w._queue_name = "new"
    w._reconnect_delay = 0.0

    def process(task):
        processed.append(task)
        w._shutdown_requested = True
        return True
    monkeypatch.setattr(w, "_process_task", process)
    return w


# A lost connection is reconnected (connect() ran only once, in run(), and
# every claim failed forever while the pod looked healthy).
def test_the_worker_reconnects_after_losing_the_connection(backend, monkeypatch):
    lost, fresh = FakeConnection(), FakeConnection([(_body(), None)])
    backend.connections = [lost, fresh]
    backend.connect()
    lost.is_open = False
    processed = []
    w = _worker(backend, monkeypatch, processed)
    w._run_loop()
    assert [t.job_id for t in processed] == [7]
    assert fresh.ch.acks == [1]


def _failing_worker(backend, monkeypatch, error, failed, fail_ok=True):
    """A v1 worker whose task fails with `error` (from _before_compute, inside
    the task path). _fail_job records into `failed`."""
    w = DistributedWorker(types.SimpleNamespace(name="cpu"), backend, worker_name="w")
    w._queue_name = "new"

    def attempt(task):
        w._shutdown_requested = True
        if error is not None:
            raise error
    monkeypatch.setattr(w, "_before_compute", attempt)
    monkeypatch.setattr(w, "_job_is_gone", lambda job_id: False)
    monkeypatch.setattr(w, "_fail_job", lambda task, reason: failed.append(reason) or fail_ok)
    return w


def _over_cap_connection(backend):
    conn = FakeConnection([(_body(), {rabbitmq.DELIVERIES_HEADER: DW.MAX_TASK_DELIVERIES})])
    backend.connections = [conn]
    backend.connect()
    return conn


# Past the cap a v1 task is attempted once more (like v1_epochs.retry_or_fail),
# and only that attempt failing fails the job.
def test_the_delivery_cap_fails_a_v1_job(backend, monkeypatch):
    conn = _over_cap_connection(backend)
    failed = []
    w = _failing_worker(backend, monkeypatch, RuntimeError("corrupt input"), failed)
    w._run_loop()
    assert len(failed) == 1 and conn.ch.acks == [1] and conn.ch.published == []


def test_an_over_cap_v1_task_that_succeeds_is_not_failed(backend, monkeypatch):
    conn = _over_cap_connection(backend)
    processed, failed = [], []
    w = _worker(backend, monkeypatch, processed)
    monkeypatch.setattr(w, "_fail_job", lambda task, reason: failed.append(reason) or True)
    w._run_loop()
    assert len(processed) == 1 and failed == [] and conn.ch.acks == [1]


# A PostgreSQL outage (peewee wraps psycopg2's connection errors) is retried
# uncounted like an S3 one, also over the cap: it would fail healthy jobs.
@pytest.mark.parametrize("error", ["peewee", "psycopg2"])
def test_a_database_outage_never_fails_a_v1_job(backend, monkeypatch, error):
    if error == "peewee":
        exc = peewee.OperationalError("server closed the connection unexpectedly")
    else:
        exc = psycopg2.OperationalError("could not connect to server")
    conn = _over_cap_connection(backend)
    failed = []
    w = _failing_worker(backend, monkeypatch, exc, failed)
    w._run_loop()
    assert failed == []
    (queue, _, props), = conn.ch.published
    assert queue == "new.retry" and props.headers[rabbitmq.DELIVERIES_HEADER] == DW.MAX_TASK_DELIVERIES


# If the job cannot be failed (database down), the task is nacked uncounted:
# left unacknowledged it would block the consumer (prefetch) until the broker's
# consumer timeout.
def test_a_v1_task_whose_job_cannot_be_failed_is_released(backend, monkeypatch):
    conn = _over_cap_connection(backend)
    failed = []
    w = _failing_worker(backend, monkeypatch, RuntimeError("corrupt input"), failed, fail_ok=False)
    w._run_loop()
    assert len(failed) == 1 and backend._inflight == {}
    (queue, _, props), = conn.ch.published
    assert queue == "new.retry" and props.headers[rabbitmq.DELIVERIES_HEADER] == DW.MAX_TASK_DELIVERIES
    assert conn.ch.acks == [1]                      # by retry_later, after the publish


# A task that failed on an S3 outage is retried without counting: a long
# MinIO outage would otherwise fail a healthy job after MAX_TASK_DELIVERIES.
def test_a_task_failing_on_an_s3_outage_is_retried_uncounted(backend, monkeypatch):
    conn = FakeConnection([(_body(), {rabbitmq.DELIVERIES_HEADER: 2})])
    backend.connections = [conn]
    backend.connect()
    w = DistributedWorker(types.SimpleNamespace(name="cpu"), backend, worker_name="w")
    w._queue_name = "new"

    def outage(task):
        w._shutdown_requested = True
        raise EndpointConnectionError(endpoint_url="http://minio:9000")
    monkeypatch.setattr(w, "_before_compute", outage)
    monkeypatch.setattr(w, "_job_is_gone", lambda job_id: False)
    w._run_loop()
    (queue, _, props), = conn.ch.published
    assert queue == "new.retry" and props.headers[rabbitmq.DELIVERIES_HEADER] == 2


# --------------------------------------------------------------------------- #
# Merger and orchestrator
# --------------------------------------------------------------------------- #

def _import(monkeypatch, name):
    patch_where_used(monkeypatch, file_service, "ensure_buckets_exist", lambda: None)
    sys.modules.pop(name, None)
    return __import__(name)


def _merge_body():
    return json.dumps({"job_id": 7, "bucket": "b", "file_key": "k", "epoch_num": 2, "package_num": 1,
                       "package_id": 70, "package_size": 4}).encode()


@pytest.fixture
def failed_jobs(monkeypatch):
    failed = []
    monkeypatch.setattr(v1_epochs, "fail_v1_job", lambda job_id, reason: failed.append(job_id) or True)
    return failed


def _broken_job_model(get=None):
    def database_down(*a):
        raise RuntimeError("database down")

    class Job:
        job_id = None
        DoesNotExist = LookupError
    Job.get = staticmethod(get or database_down)
    return Job


# A failed merge is retried later at the back of the queue (a nack-requeue
# returned it to the head: a hot loop that starved the other jobs' merges),
# and past the cap fails its job instead of retrying forever.
def test_a_failing_merge_is_retried_later_then_fails_the_job(monkeypatch, failed_jobs):
    M = _import(monkeypatch, "merger")
    monkeypatch.setattr(M, "Job", _broken_job_model())
    ch = FakeChannel(None)
    M.process_merge(ch, types.SimpleNamespace(delivery_tag=1), pika.BasicProperties(), _merge_body())
    (queue, _, props), = ch.published
    assert queue == "to_merge.retry" and props.headers[rabbitmq.DELIVERIES_HEADER] == 1
    assert ch.acks == [1] and failed_jobs == []

    last = pika.BasicProperties(headers={rabbitmq.DELIVERIES_HEADER: rabbitmq.MAX_DELIVERIES - 1})
    M.process_merge(ch, types.SimpleNamespace(delivery_tag=2), last, _merge_body())
    assert failed_jobs == [7] and ch.nacks == [(2, False)] and len(ch.published) == 1


def test_a_merge_failing_on_an_s3_outage_is_retried_without_counting(monkeypatch, failed_jobs):
    """The package downloads failed as a plain RuntimeError, so a MinIO
    outage longer than MAX_DELIVERIES retry delays failed a healthy job."""
    M = _import(monkeypatch, "merger")

    class S3:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def download_file(self, bucket, key):
            raise EndpointConnectionError(endpoint_url="http://minio:9000")
    monkeypatch.setattr(M, "AsyncS3Service", S3)
    pkgs = [types.SimpleNamespace(package_id=i, file_key=f"k{i}") for i in range(2)]
    with pytest.raises(M.PackageDownloadError) as err:
        asyncio.run(M.download_packages_async(pkgs))
    assert err.value.transient

    def outage(*a, **k):
        raise err.value
    monkeypatch.setattr(M, "Job", _broken_job_model(outage))
    ch = FakeChannel(None)
    last = pika.BasicProperties(headers={rabbitmq.DELIVERIES_HEADER: rabbitmq.MAX_DELIVERIES - 1})
    M.process_merge(ch, types.SimpleNamespace(delivery_tag=2), last, _merge_body())
    (queue, _, props), = ch.published
    assert queue == "to_merge.retry" and failed_jobs == []
    assert props.headers[rabbitmq.DELIVERIES_HEADER] == rabbitmq.MAX_DELIVERIES - 1   # not counted


# A dead channel in the error path reconnects (main) instead of crashing.
def test_a_dead_channel_is_left_to_the_reconnect(monkeypatch):
    M = _import(monkeypatch, "merger")
    monkeypatch.setattr(M, "Job", _broken_job_model())
    ch = FakeChannel(None)

    def dead(**k):
        raise pika.exceptions.ChannelWrongStateError("closed")
    ch.basic_publish = dead
    with pytest.raises(pika.exceptions.ChannelWrongStateError):
        M.process_merge(ch, types.SimpleNamespace(delivery_tag=1), pika.BasicProperties(), _merge_body())


def _orchestrator_with_job(monkeypatch, job):
    O = _import(monkeypatch, "orchestrator")

    class Col:
        def __eq__(self, other):
            return True

        def __and__(self, other):
            return self

    class Job:
        job_id = status = Col()
        DoesNotExist = LookupError
        updated = 1

        @staticmethod
        def select():
            return types.SimpleNamespace(where=lambda *a: types.SimpleNamespace(
                for_update=lambda: types.SimpleNamespace(get=lambda: job)))

        @staticmethod
        def update(**kw):
            return types.SimpleNamespace(where=lambda *a: types.SimpleNamespace(execute=lambda: Job.updated))
    monkeypatch.setattr(O, "Job", Job)
    monkeypatch.setattr(O, "db", types.SimpleNamespace(atomic=contextlib.nullcontext))
    return O, Job


def _finished_body(epoch):
    return json.dumps({"job_id": 4, "bucket": "e", "file_key": "k", "epoch_num": epoch, "a_max": 0.0}).encode()


# A job paused during the combination is not completed, and its snapshots are
# kept: it still points at the last epoch's file, which a resume re-combines.
def test_completion_that_lost_the_race_with_a_pause_keeps_the_snapshots(monkeypatch):
    job = types.SimpleNamespace(job_id=4, results_file_key="last.csv", snapshot_interval=1, num_epochs=5,
                                updated_at=None)
    O, Job = _orchestrator_with_job(monkeypatch, job)
    Job.updated = 0
    monkeypatch.setattr(O, "combine_epoch_snapshots", lambda **kw: "combined.csv")
    deleted = []
    monkeypatch.setattr(O, "delete_files_by_prefix", lambda *a: deleted.append(a) or 0)
    O._complete_job(job)
    assert deleted == []


# Transient S3 errors while completing are retried after the delay, without
# counting towards the cap (an outage must not fail a finished job).
def test_a_transient_s3_error_is_retried_later_uncounted(monkeypatch, failed_jobs):
    job = types.SimpleNamespace(job_id=4, current_epoch=5, num_epochs=5, status="IN_PROGRESS",
                                results_file_key="last.csv", snapshot_interval=1, updated_at=None,
                                save=lambda: None)
    O, _ = _orchestrator_with_job(monkeypatch, job)

    def combine(**kw):
        raise ClientError({"Error": {"Code": "SlowDown", "Message": "SlowDown"}}, "GetObject")
    monkeypatch.setattr(O, "combine_epoch_snapshots", combine)
    ch = FakeChannel(None)
    props = pika.BasicProperties(headers={rabbitmq.DELIVERIES_HEADER: rabbitmq.MAX_DELIVERIES + 3})
    O.process_finalize_epoch(ch, types.SimpleNamespace(delivery_tag=1, redelivered=False), props,
                             _finished_body(5))
    (queue, _, out), = ch.published
    assert queue == "finished_epoch.retry" and ch.acks == [1] and failed_jobs == []
    assert out.headers[rabbitmq.DELIVERIES_HEADER] == rabbitmq.MAX_DELIVERIES + 3


# A retried copy is our own redelivery: its recorded epoch's successor is re-queued.
def test_a_retried_epoch_counts_as_a_redelivery(monkeypatch):
    job = types.SimpleNamespace(job_id=4, current_epoch=3, num_epochs=10, status="IN_PROGRESS",
                                time_step=0.01, adaptive_enabled=False, save=lambda: None)
    O, _ = _orchestrator_with_job(monkeypatch, job)
    queued = []
    monkeypatch.setattr(O, "queue_v1_epoch", lambda j, ch, dt: queued.append(j.current_epoch))
    ch = FakeChannel(None)
    props = pika.BasicProperties(headers={rabbitmq.DELIVERIES_HEADER: 1})
    O.process_finalize_epoch(ch, types.SimpleNamespace(delivery_tag=1, redelivered=False), props,
                             _finished_body(3))
    assert queued == [3] and ch.acks == [1]


# The step the next epoch runs at is stored with it, so a resume continues
# with an adaptive job's current step
def test_the_epoch_advance_records_the_next_step(monkeypatch):
    job = types.SimpleNamespace(job_id=4, current_epoch=2, num_epochs=10, status="IN_PROGRESS",
                                time_step=0.01, adaptive_enabled=True, adaptive_eta=0.02, softening_sq=1.0,
                                adaptive_dt_min=1e-6, adaptive_dt_max=0.05, num_packages=2, package_size=5,
                                force_algorithm="direct", barnes_hut_theta=0.5, integration_method="rk4",
                                gpu_enabled=False, next_time_step=None, save=lambda: None)
    O, _ = _orchestrator_with_job(monkeypatch, job)

    class Package(types.SimpleNamespace):
        package_id = 1

        def save(self):
            pass
    monkeypatch.setattr(O, "Package", Package)
    ch = FakeChannel(None)
    monkeypatch.setattr(O, "channel", ch)      # the module's consuming channel publishes
    body = json.dumps({"job_id": 4, "bucket": "e", "file_key": "k", "epoch_num": 3, "a_max": 400.0}).encode()
    O.process_finalize_epoch(ch, types.SimpleNamespace(delivery_tag=1, redelivered=False), None, body)
    expected = O._next_time_step(job, 400.0)
    assert expected != job.time_step and job.next_time_step == expected
    assert {json.loads(b)["time_step"] for _, b, _ in ch.published} == {expected}


# v1 publishes wait for the broker's confirmation: a refused message raises
# instead of being lost while its delivery is acknowledged
def test_the_v1_worker_channel_confirms_its_publishes(backend):
    conn = FakeConnection()
    backend.connections = [conn]
    backend.connect()
    assert conn.ch.confirming
