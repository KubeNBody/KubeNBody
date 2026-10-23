# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Job submission (v1 routes/jobs.py, v2 v2/api_v2.py, both through
utils/input_files.py), input file deletion, RabbitMQ publishing of the API,
comparison deduplication and the monitoring endpoints, over SQLite tables
(no test talks to PostgreSQL)."""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import io
import json
import threading
import time
import types

import anyio.to_thread
import pika.exceptions
import pytest
from fastapi import HTTPException
from peewee import (
    AutoField,
    BooleanField,
    CharField,
    DateTimeField,
    FloatField,
    ForeignKeyField,
    IntegerField,
    Model,
    SqliteDatabase,
    TextField,
)

import api as A
import publisher as P
import rabbitmq
import routes.comparison as C
import routes.files as F
import routes.jobs as J
import routes.monitoring as M
import utils.file_parsing as FP
import utils.input_files as IF
import v2.api_v2 as V2
from api_schemas import ComparisonJobSubmitRequest
from rabbitmq import BrokerUnavailable


CSV = "n,idBody,m,x,y,z,vx,vy,vz\n1,1,1.0,0,0,0,0,0,0\n2,2,1.0,1,0,0,0,1,0\n3,3,1.0,2,0,0,0,1,0\n"


@pytest.fixture
def tables(monkeypatch, tmp_path):
    """Job, Package, InputFile and ComparisonJob in an SQLite database, put
    in place of the models of every module under test. S3 is a dict."""
    db = SqliteDatabase(str(tmp_path / "api.db"))     # a file: routes query from worker threads too

    class Base(Model):
        class Meta:
            database = db

    class InputFile(Base):
        file_id = AutoField()
        user_friendly_name = CharField(unique=True)
        original_filename = CharField()
        s3_key = CharField(unique=True)
        s3_bucket = CharField(default="inputfiles")
        file_size = IntegerField()
        num_bodies = IntegerField()
        created_at = DateTimeField(default=dt.datetime.now)
        last_used_at = DateTimeField(default=dt.datetime.now)
        usage_count = IntegerField(default=0)
        description = CharField(null=True)

    class Job(Base):
        job_id = AutoField()
        status = CharField(default="IN_PROGRESS")
        job_type = CharField(default="distributed")
        num_epochs = IntegerField(default=10)
        current_epoch = IntegerField(default=0)
        package_size = IntegerField(default=0)
        num_packages = IntegerField(default=0)
        input_file_key = CharField(null=True)
        input_bucket = CharField(null=True)
        input_file_id = IntegerField(null=True)
        time_step = FloatField(default=0.01)
        snapshot_interval = IntegerField(default=1)
        filename = CharField(null=True)
        file_size = IntegerField(null=True)
        gpu_enabled = BooleanField(default=False)
        force_algorithm = CharField(default="direct")
        barnes_hut_theta = FloatField(default=0.5)
        softening_sq = FloatField(default=1e-9)
        integration_method = CharField(default="rk4")
        adaptive_enabled = BooleanField(default=False)
        adaptive_eta = FloatField(null=True)
        adaptive_dt_min = FloatField(null=True)
        adaptive_dt_max = FloatField(null=True)
        metadata_json = TextField(null=True)
        description = TextField(null=True)
        partition_strategy = CharField(default="linear")
        partition_seed = IntegerField(default=0)
        recovery_count = IntegerField(default=0)
        completed_at = DateTimeField(null=True)
        updated_at = DateTimeField(null=True)
        worker_id = CharField(null=True)
        leased_at = DateTimeField(null=True)

    class Package(Base):
        package_id = AutoField()
        package_number = IntegerField()
        epoch_num = IntegerField()
        job = ForeignKeyField(Job)
        status = CharField()
        file_key = CharField(null=True)

    class ComparisonJob(Base):
        comparison_id = AutoField()
        reference_job_id = IntegerField()
        comparison_job_ids = CharField()
        max_snapshots = IntegerField(null=True)
        status = CharField(default="PENDING")
        error_message = CharField(null=True)
        created_at = DateTimeField(default=lambda: dt.datetime.now(dt.timezone.utc))
        updated_at = DateTimeField(default=lambda: dt.datetime.now(dt.timezone.utc))
        started_at = DateTimeField(null=True)
        computation_time_ms = IntegerField(null=True)
        results_file_key = CharField(null=True)
        jobs_compared = IntegerField(default=0)
        jobs_skipped = IntegerField(default=0)
        job_fingerprints = TextField(null=True)

    db.create_tables([InputFile, Job, Package, ComparisonJob])
    s3 = {}
    for mod in (IF, F, C):
        monkeypatch.setattr(mod, "InputFile", InputFile, raising=False)
    for mod in (J, F, C, V2):
        monkeypatch.setattr(mod, "Job", Job)
    monkeypatch.setattr(J, "Package", Package)
    monkeypatch.setattr(J, "db", db)
    monkeypatch.setattr(C, "ComparisonJob", ComparisonJob)
    monkeypatch.setattr(IF, "utcnow", dt.datetime.now)                    # naive: SQLite drops the zone
    monkeypatch.setattr(IF, "upload_file", lambda bucket, key, f: s3.__setitem__(key, f.getvalue()))
    monkeypatch.setattr(IF, "download_file", lambda bucket, key: s3[key])
    yield types.SimpleNamespace(db=db, InputFile=InputFile, Job=Job, Package=Package,
                                ComparisonJob=ComparisonJob, s3=s3)
    db.close()


def _upload(content=CSV, filename="bodies.csv", size=None):
    data = content.encode() if isinstance(content, str) else content
    return types.SimpleNamespace(filename=filename, size=size, file=io.BytesIO(data))


def _defaults(fn, **over):
    """Every parameter of a route at its default value, overridden by `over`."""
    kw = {}
    for name, p in inspect.signature(fn).parameters.items():
        d = p.default
        kw[name] = getattr(d, "default", d) if d is not inspect.Parameter.empty else None
    kw.update(over)
    return kw


def _v1_submit(**over):
    # time_step: its default comes from models (stubbed in the tests)
    kw = _defaults(J.submit_job, job_type="distributed", package_size=2, num_epochs=5, snapshot_interval=1,
                   time_step=0.01, current_user=None)
    kw.update(over)
    return asyncio.run(J.submit_job(**kw))


def _v2_submit(monkeypatch, **over):
    created = []
    monkeypatch.setattr(V2, "get_orchestrator", lambda: types.SimpleNamespace(
        create_job_with_bodies=lambda **kw: created.append(kw)))
    kw = _defaults(V2.create_job, current_user=types.SimpleNamespace(username="u"))
    kw.update(over)
    return V2.create_job(**kw), created


@pytest.fixture
def published(monkeypatch):
    """The v1 routes' publishes, as (queue, body) pairs."""
    out = []

    async def publish(messages):
        out.extend(messages)
    monkeypatch.setattr(J, "publish_persistent", publish)
    return out


# --------------------------------------------------------------------------- #
# Upload size
# --------------------------------------------------------------------------- #

def test_v2_upload_over_the_limit_is_refused_before_it_is_read(tables, monkeypatch):
    """The v2 upload was read whole, then compared with the limit."""
    monkeypatch.setattr(IF, "MAX_UPLOAD_BYTES", 100)
    declared = _upload(b"x" * 1000, size=1000)
    declared.file.read = lambda *a: pytest.fail("read although the declared size is over the limit")
    with pytest.raises(HTTPException) as e:
        _v2_submit(monkeypatch, file=declared)
    assert e.value.status_code == 413
    undeclared = _upload(b"x" * 1000)                         # no size sent: read one byte past the limit
    with pytest.raises(HTTPException) as e:
        _v2_submit(monkeypatch, file=undeclared)
    assert e.value.status_code == 413 and undeclared.file.tell() == 101
    assert tables.InputFile.select().count() == 0


# --------------------------------------------------------------------------- #
# Validation before the file is stored (v2)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", [dict(integration_method="rk5"), dict(force_algorithm="nope"),
                                 dict(partition_strategy="hilbert"), dict(snapshot_interval=50, num_epochs=10)])
def test_v2_rejects_bad_parameters_before_storing_the_file(tables, monkeypatch, bad):
    """A typo returned 400 after the library file was created, and the
    corrected resubmission then got 409 (name taken)."""
    with pytest.raises(HTTPException) as e:
        _v2_submit(monkeypatch, file=_upload(), **bad)
    assert e.value.status_code == 400
    assert tables.InputFile.select().count() == 0 and tables.s3 == {}
    out, created = _v2_submit(monkeypatch, file=_upload())     # the corrected resubmission
    assert out.num_bodies == 3 and created and tables.InputFile.select().count() == 1


def test_v2_and_v1_record_the_library_file_of_their_job(tables, monkeypatch, published):
    out, created = _v2_submit(monkeypatch, file=_upload(), user_friendly_name="plummer")
    f = tables.InputFile.get()
    assert (f.user_friendly_name, f.s3_key, f.num_bodies) == ("plummer", out.file_key, 3)
    job = tables.Job.get(tables.Job.job_id == out.job_id)
    assert job.input_file_id == f.file_id and job.filename == "bodies.csv"
    assert len(created[0]["bodies"]) == 3 and isinstance(created[0]["bodies"][0], dict)
    out = _v1_submit(file_id=f.file_id)
    assert out.num_bodies == 3 and tables.Job.get(tables.Job.job_id == out.job_id).input_file_id == f.file_id
    assert tables.InputFile.get().usage_count == 2


# --------------------------------------------------------------------------- #
# Name conflicts
# --------------------------------------------------------------------------- #

def test_v1_force_new_with_a_taken_name_creates_a_new_file(tables, published):
    """force_new skipped the conflict check and reused the UNIQUE name: 500."""
    _v1_submit(file=_upload(), user_friendly_name="run")
    conflict = _v1_submit(file=_upload(), user_friendly_name="run")
    assert conflict.status_code == 409 and json.loads(conflict.body)["error"] == "name_conflict"
    out = _v1_submit(file=_upload(), user_friendly_name="run", force_new=True)
    names = sorted(f.user_friendly_name for f in tables.InputFile.select())
    assert len(names) == 2 and names[0] == "run" and names[1].startswith("run_")
    assert out.file_key.endswith(f"/{names[1]}.csv")


@pytest.mark.parametrize("api", ["v1", "v2"])
def test_a_name_taken_concurrently_is_409(tables, monkeypatch, published, api):
    """Two submissions of the same name: the second's insert violates the
    unique name (it passed the check before the first inserted)."""
    _v1_submit(file=_upload(), user_friendly_name="run")
    real = tables.InputFile.get_or_none
    calls = []

    def check_misses_the_concurrent_row(*a, **k):
        calls.append(1)
        return None if len(calls) == 1 else real(*a, **k)
    monkeypatch.setattr(IF.InputFile, "get_or_none", check_misses_the_concurrent_row)
    out = (_v1_submit(file=_upload(), user_friendly_name="run") if api == "v1"
           else _v2_submit(monkeypatch, file=_upload(), user_friendly_name="run")[0])
    assert out.status_code == 409 and json.loads(out.body)["existing_file"]["user_friendly_name"] == "run"
    assert tables.InputFile.select().count() == 1


def test_both_endpoints_record_a_job_the_same_way(tables, monkeypatch, published):
    """The columns the two submit endpoints share come from one place, from
    the same arguments."""
    recorded = []
    for module in (J, V2):
        real = module.job_fields
        monkeypatch.setattr(module, "job_fields",
                            lambda source, real=real, **kw: recorded.append(real(source, **kw)) or recorded[-1])
    params = dict(num_epochs=5, snapshot_interval=1, time_step=0.01, package_size=2,
                  force_algorithm="barnes_hut", barnes_hut_theta=0.7, softening_sq=0.01,
                  integration_method="velocity_verlet", adaptive_enabled=True, adaptive_eta=0.05,
                  adaptive_dt_max=0.02, job_description="d", job_name="  n  ")
    _v1_submit(file=_upload(), user_friendly_name="a", **params)
    _v2_submit(monkeypatch, file=_upload(), user_friendly_name="b", **params)
    v1, v2 = ({k: v for k, v in r.items() if k not in ("input_file_key", "input_file_id", "metadata_json")}
              for r in recorded)
    assert v1 == v2
    assert (v1["barnes_hut_theta"], v1["adaptive_dt_min"], v1["name"]) == (0.7, None, "n")


def test_space_separated_files_are_capped_too(monkeypatch):
    monkeypatch.setattr(FP, "MAX_BODIES_PER_FILE", 2)
    with pytest.raises(ValueError, match="maximum body count"):
        FP.validate_csv_data("1 0 0 0 0 0 0\n1 1 0 0 0 0 0\n1 2 0 0 0 0 0\n")


# --------------------------------------------------------------------------- #
# Publishing (v1)
# --------------------------------------------------------------------------- #

def test_a_publish_failing_partway_deletes_the_job_and_its_packages(tables, monkeypatch):
    """Packages already queued then find no package row: the v1 worker drops
    them (Package.update matches nothing, no merge)."""
    sent = []

    async def publish(messages):
        sent.append(messages[0])
        raise RuntimeError("NackError")
    monkeypatch.setattr(J, "publish_persistent", publish)
    with pytest.raises(HTTPException) as e:
        _v1_submit(file=_upload(), package_size=1)
    assert e.value.status_code == 500 and len(sent) == 1
    assert tables.Job.select().count() == 0 and tables.Package.select().count() == 0
    assert tables.InputFile.select().count() == 1               # the library file stays


def test_a_distributed_submit_publishes_every_package(tables, published):
    out = _v1_submit(file=_upload(), package_size=2)
    packages = list(tables.Package.select().order_by(tables.Package.package_number))
    assert [(q, json.loads(b)["package_id"]) for q, b in published] == [("new", p.package_id) for p in packages]
    assert tables.Job.get(tables.Job.job_id == out.job_id).num_packages == 2


class FakeChannel:
    def __init__(self, fail_on=None, delay=0.0):
        self.is_closed, self.confirming, self.sent = False, False, []
        self.fail_on, self.delay = fail_on, delay
        self.threads = set()

    def confirm_delivery(self):
        self.confirming = True

    def basic_publish(self, exchange, routing_key, body, properties, mandatory=False):
        self.threads.add(threading.current_thread().name)
        time.sleep(self.delay)
        if body == self.fail_on:
            raise pika.exceptions.NackError([])
        assert self.confirming and mandatory and properties.delivery_mode == 2
        self.sent.append((routing_key, body))


@pytest.fixture
def api_channel(monkeypatch):
    holder = {}

    def connect(**kw):
        channel = holder["channel"]
        return types.SimpleNamespace(is_closed=False, is_open=True, channel=lambda: channel,
                                     process_data_events=lambda time_limit=None: None)
    monkeypatch.setattr(P, "rabbitmq_connection", None)
    monkeypatch.setattr(P, "rabbitmq_channel", None)
    monkeypatch.setattr(P, "publisher_connection", connect)
    monkeypatch.setattr(P, "_reconnect_not_before", 0.0)
    monkeypatch.setattr(P, "ensure_rabbitmq_queues", lambda ch: None)
    return A, holder


def test_publishes_are_confirmed_and_off_the_event_loop(api_channel):
    """The publishes ran on the event loop (a broker in flow control stalled
    every request) without confirms (a lost publish went unnoticed)."""
    A, holder = api_channel
    holder["channel"] = channel = FakeChannel(delay=0.2)

    async def main():
        ticks = 0

        async def tick():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1
        ticker = asyncio.create_task(tick())
        await P.publish_persistent([("new", "a"), ("new", "b")])
        ticker.cancel()
        return ticks
    assert asyncio.run(main()) >= 10                   # the loop kept running meanwhile
    assert channel.sent == [("new", "a"), ("new", "b")]
    assert threading.main_thread().name not in channel.threads


def test_a_refused_publish_raises(api_channel):
    A, holder = api_channel
    holder["channel"] = channel = FakeChannel(fail_on="b")
    with pytest.raises(pika.exceptions.NackError):
        asyncio.run(P.publish_persistent([("new", "a"), ("new", "b"), ("new", "c")]))
    assert channel.sent == [("new", "a")]


def _connection_errors():
    return [pika.exceptions.AMQPHeartbeatTimeout("no heartbeat"), pika.exceptions.StreamLostError("reset"),
            pika.exceptions.ConnectionBlockedTimeout(), pika.exceptions.ChannelClosedByBroker(406, "PRECONDITION_FAILED"),
            pika.exceptions.ConnectionClosedByBroker(320, "CONNECTION_FORCED"), pika.exceptions.ChannelWrongStateError("closed")]


class DyingChannel(FakeChannel):
    """A channel whose connection dies in the middle of a publish."""

    def __init__(self, error):
        super().__init__()
        self.error = error

    def basic_publish(self, **kw):
        raise self.error


@pytest.mark.parametrize("error", _connection_errors(), ids=lambda e: type(e).__name__)
def test_a_connection_dying_mid_publish_is_broker_unavailable_and_reconnects(api_channel, error):
    """Only a failed reconnect was BrokerUnavailable: a connection lost during
    the publish (heartbeat timeout, reset, a broker alarm outlasting
    blocked_connection_timeout) surfaced as pika's error, a 500."""
    A, holder = api_channel
    holder["channel"] = DyingChannel(error)
    with pytest.raises(BrokerUnavailable) as e:
        asyncio.run(P.publish_persistent([("new", "a")]))
    assert e.value.__cause__ is error
    assert P.rabbitmq_connection is None and P.rabbitmq_channel is None
    holder["channel"] = channel = FakeChannel()
    asyncio.run(P.publish_persistent([("new", "a")]))           # a new connection
    assert channel.sent == [("new", "a")]


@pytest.mark.parametrize("error", [pika.exceptions.NackError([]), pika.exceptions.UnroutableError([]),
                                   RuntimeError("database down")], ids=lambda e: type(e).__name__)
def test_a_refused_message_or_other_error_passes_through_and_keeps_the_connection(api_channel, error):
    A, holder = api_channel
    holder["channel"] = DyingChannel(error)
    with pytest.raises(type(error)) as e:
        asyncio.run(P.publish_persistent([("new", "a")]))
    assert not isinstance(e.value, BrokerUnavailable) and P.rabbitmq_channel is holder["channel"]


@pytest.mark.parametrize("error,status", [(pika.exceptions.StreamLostError("reset"), 503),
                                          (pika.exceptions.UnroutableError([]), 500)])
def test_a_v1_submit_whose_publish_fails_is_discarded_with_503_only_for_the_broker(
        tables, api_channel, error, status):
    A, holder = api_channel
    holder["channel"] = DyingChannel(error)
    with pytest.raises(HTTPException) as e:
        _v1_submit(file=_upload())
    assert e.value.status_code == status and tables.Job.select().count() == 0


@pytest.mark.parametrize("error,status", [(pika.exceptions.AMQPHeartbeatTimeout("x"), 503),
                                          (pika.exceptions.NackError([]), 500)])
def test_a_resume_whose_publish_fails_leaves_the_job_paused(tables, api_channel, monkeypatch, error, status):
    """Every failure was a 503 "try again", also a refused message."""
    monkeypatch.setattr(J, "utcnow", dt.datetime.now)                    # naive: SQLite drops the zone
    A, holder = api_channel
    holder["channel"] = DyingChannel(error)
    job = tables.Job.create(status="PAUSED", job_type="monolithic", input_file_key="k", input_bucket="b")
    with pytest.raises(HTTPException) as e:
        asyncio.run(J.resume_job(job.job_id, current_user=None))
    assert e.value.status_code == status and "still paused" in e.value.detail
    assert tables.Job.get_by_id(job.job_id).status == "PAUSED"


def test_publishes_from_concurrent_requests_do_not_interleave(api_channel):
    A, holder = api_channel
    holder["channel"] = channel = FakeChannel(delay=0.01)

    async def main():
        await asyncio.gather(*(P.publish_persistent([(f"q{i}", "1"), (f"q{i}", "2")]) for i in range(5)))
    asyncio.run(main())
    queues = [q for q, _ in channel.sent]
    assert all(queues[i] == queues[i + 1] for i in range(0, len(queues), 2))


def test_a_submit_while_the_broker_is_unreachable_is_a_503(tables, monkeypatch):
    async def publish(messages):
        raise BrokerUnavailable("RabbitMQ unreachable")
    monkeypatch.setattr(J, "publish_persistent", publish)
    with pytest.raises(HTTPException) as e:
        _v1_submit(file=_upload())
    assert e.value.status_code == 503 and tables.Job.select().count() == 0


def test_the_publisher_connects_once_with_short_timeouts_and_its_own_heartbeat(monkeypatch):
    """The reconnect used get_rabbitmq_connection's parameters: 10 attempts
    3 s apart (30-130 s under the publish lock) and RABBITMQ_HEARTBEAT, 1800 s
    in prod, so a half-open connection held a confirm wait for half an hour."""
    params = []
    monkeypatch.setattr(rabbitmq.pika, "BlockingConnection", lambda p: params.append(p) or "conn")
    monkeypatch.setattr(A.app_settings.rabbitmq, "heartbeat", 1800)
    assert rabbitmq.publisher_connection(heartbeat_s=P.PUBLISHER_HEARTBEAT_S) == "conn"
    p, = params
    assert isinstance(p, pika.ConnectionParameters) and p.connection_attempts == 1
    assert p.heartbeat == P.PUBLISHER_HEARTBEAT_S <= 60
    assert max(p.socket_timeout, p.stack_timeout, p.blocked_connection_timeout) <= 10


def test_an_unreachable_broker_fails_publishes_fast_without_starving_the_default_executor(api_channel, monkeypatch):
    """One short connection attempt, then the breaker fails the queued
    publishes at once, on the publisher's own threads: a minutes-long reconnect
    per publish, each holding a default-executor thread, would time out
    /api/ready."""
    A, _ = api_channel
    attempts = []

    def refuse(**kw):
        attempts.append(threading.current_thread().name)
        time.sleep(0.2)
        raise pika.exceptions.AMQPConnectionError("refused")
    monkeypatch.setattr(P, "publisher_connection", refuse)

    async def main():
        publishes = [asyncio.ensure_future(P.publish_persistent([("new", str(i))])) for i in range(50)]
        await asyncio.sleep(0.05)
        start = time.monotonic()
        await asyncio.to_thread(lambda: None)                 # the readiness probe's slot
        probe = time.monotonic() - start
        results = await asyncio.gather(*publishes, return_exceptions=True)
        return probe, results, time.monotonic() - start
    probe, results, total = asyncio.run(main())
    assert probe < 0.1
    assert all(isinstance(r, BrokerUnavailable) for r in results)
    assert len(attempts) == 1 and total < 2                   # one attempt, the rest failed fast
    assert all(name.startswith("rabbitmq-publish") for name in attempts)


def test_a_publish_stuck_on_the_broker_turns_waiting_ones_into_503s(api_channel, monkeypatch):
    """A confirm wait on a half-open connection held _publish_lock and every
    other publish queued behind it without limit."""
    A, holder = api_channel
    monkeypatch.setattr(P, "PUBLISH_WAIT_S", 0.3)
    release = threading.Event()

    class Stuck(FakeChannel):
        def basic_publish(self, **kw):
            if kw["body"] == "stuck":
                release.wait(5)
            return super().basic_publish(**kw)
    holder["channel"] = channel = Stuck()

    async def main():
        stuck = asyncio.ensure_future(P.publish_persistent([("new", "stuck")]))
        await asyncio.sleep(0.05)
        start = time.monotonic()
        waiting = await asyncio.gather(*(P.publish_persistent([("new", str(i))]) for i in range(5)),
                                       return_exceptions=True)
        waited = time.monotonic() - start
        release.set()
        await stuck
        return waiting, waited
    waiting, waited = asyncio.run(main())
    assert all(isinstance(r, BrokerUnavailable) for r in waiting) and waited < 1.5
    assert channel.sent == [("new", "stuck")]


def test_the_breaker_lets_a_publish_reconnect_after_the_backoff(api_channel, monkeypatch):
    A, holder = api_channel
    holder["channel"] = channel = FakeChannel()
    monkeypatch.setattr(P, "RECONNECT_BACKOFF_S", 0.2)
    real = P.publisher_connection

    def refuse(**kw):
        raise pika.exceptions.AMQPConnectionError("refused")
    monkeypatch.setattr(P, "publisher_connection", refuse)
    for _ in range(2):
        with pytest.raises(BrokerUnavailable):
            asyncio.run(P.publish_persistent([("new", "a")]))
    monkeypatch.setattr(P, "publisher_connection", real)
    with pytest.raises(BrokerUnavailable, match="recent reconnect failed"):
        asyncio.run(P.publish_persistent([("new", "a")]))
    time.sleep(0.25)
    asyncio.run(P.publish_persistent([("new", "a")]))
    assert channel.sent == [("new", "a")]


def test_the_keepalive_pumps_an_idle_connection_but_never_a_busy_one(monkeypatch):
    pumped = []
    conn = types.SimpleNamespace(is_closed=False, process_data_events=lambda time_limit=None: pumped.append(1))
    monkeypatch.setattr(P, "rabbitmq_connection", conn)
    P._pump_idle_publisher()
    assert pumped == [1]
    with P._publish_lock:
        P._pump_idle_publisher()                              # a publish holds it: skipped
    assert pumped == [1]


def test_handler_threadpool_fits_the_database_pool(monkeypatch):
    monkeypatch.setattr(A.app_settings.database, "max_connections", 20)

    async def main():
        A._limit_threadpool()
        return anyio.to_thread.current_default_thread_limiter().total_tokens
    assert asyncio.run(main()) == 20 - A.DB_CONNECTIONS_RESERVED


# --------------------------------------------------------------------------- #
# Deleting an input file
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("link", ["id", "key"])
def test_a_file_used_by_a_completed_job_is_not_deleted(tables, monkeypatch, link):
    """A completed job's comparisons read its input file (initial state)."""
    monkeypatch.setattr(F, "delete_s3_file", lambda b, k: pytest.fail("S3 object deleted"))
    f = tables.InputFile.create(user_friendly_name="f", original_filename="f.csv", s3_key="files/1/f.csv",
                                file_size=1, num_bodies=1)
    for _ in range(2):
        tables.Job.create(status="COMPLETED", input_file_id=f.file_id if link == "id" else None,
                          input_file_key=f.s3_key if link == "key" else "legacy.csv")
    with pytest.raises(HTTPException) as e:
        F.delete_file(f.file_id, current_user=None)
    assert e.value.status_code == 409 and "2 job(s)" in e.value.detail
    assert tables.InputFile.select().count() == 1


def test_an_unused_file_is_deleted(tables, monkeypatch):
    deleted = []
    monkeypatch.setattr(F, "delete_s3_file", lambda b, k: deleted.append(k))
    f = tables.InputFile.create(user_friendly_name="f", original_filename="f.csv", s3_key="files/1/f.csv",
                                file_size=1, num_bodies=1)
    tables.Job.create(status="COMPLETED", input_file_key="files/2/other.csv")
    assert F.delete_file(f.file_id, current_user=None)["file_id"] == f.file_id
    assert deleted == ["files/1/f.csv"] and tables.InputFile.select().count() == 0


# --------------------------------------------------------------------------- #
# Comparison staleness and deduplication
# --------------------------------------------------------------------------- #

def _completed_jobs(tables):
    done = dt.datetime(2026, 1, 1)
    return [tables.Job.create(status="COMPLETED", completed_at=done, input_file_key="files/1/f.csv")
            for _ in range(2)]


def _save_comparison(tables, monkeypatch, ref, cmp):
    async def publish(messages):
        pass
    monkeypatch.setattr(C, "publish_persistent", publish)
    request = ComparisonJobSubmitRequest(reference_job_id=ref.job_id, comparison_job_ids=[cmp.job_id],
                                         max_snapshots=10)
    return asyncio.run(C.create_comparison_job(request, current_user=None))


def test_a_recovered_job_makes_its_comparison_stale_and_not_reused(tables, monkeypatch):
    """The fingerprint held only settings that a recovery keeps, so the rerun
    job's old comparison was neither stale nor recomputed."""
    ref, cmp = _completed_jobs(tables)
    first = _save_comparison(tables, monkeypatch, ref, cmp)
    tables.ComparisonJob.update(status="COMPLETED").execute()
    assert _save_comparison(tables, monkeypatch, ref, cmp).comparison_id == first.comparison_id
    tables.Job.update(recovery_count=1, completed_at=dt.datetime(2026, 2, 1)).where(
        tables.Job.job_id == cmp.job_id).execute()
    stale, reason = C._check_staleness(tables.ComparisonJob.get())
    assert stale and "recovery_count" in reason
    second = _save_comparison(tables, monkeypatch, ref, cmp)
    assert second.comparison_id != first.comparison_id and not second.stale


def test_old_fingerprints_compare_only_their_own_keys(tables):
    ref, cmp = _completed_jobs(tables)
    old = {str(j.job_id): {"algorithm": "direct", "time_step": 0.01, "num_epochs": 10} for j in (ref, cmp)}
    old["_version"] = C.COMPARISON_ALGORITHM_VERSION
    comp = tables.ComparisonJob.create(reference_job_id=ref.job_id, comparison_job_ids=json.dumps([cmp.job_id]),
                                       status="COMPLETED", job_fingerprints=json.dumps(old))
    assert C._check_staleness(comp) == (False, None)


@pytest.mark.parametrize("version", [None, "missing", 2])
def test_a_comparison_from_an_older_method_is_stale_and_not_reused(tables, monkeypatch, version):
    """Comparisons computed under older rules (momentum drift against |P0|,
    the first max_snapshots snapshots) were reused for identical requests and
    never flagged stale."""
    ref, cmp = _completed_jobs(tables)
    first = _save_comparison(tables, monkeypatch, ref, cmp)
    row = tables.ComparisonJob.get_by_id(first.comparison_id)
    assert json.loads(row.job_fingerprints)["_version"] == C.COMPARISON_ALGORITHM_VERSION
    fps = json.loads(row.job_fingerprints)
    if version == "missing":
        del fps["_version"]
    else:
        fps["_version"] = version
    tables.ComparisonJob.update(status="COMPLETED", job_fingerprints=json.dumps(fps)
                                if version is not None else None).execute()
    assert C._check_staleness(tables.ComparisonJob.get()) == (True, "computed with an older comparison method")
    second = _save_comparison(tables, monkeypatch, ref, cmp)
    assert second.comparison_id != first.comparison_id and not second.stale


@pytest.mark.parametrize("error,status", [("broker", 503), ("unroutable", 500)])
def test_a_saved_comparison_that_cannot_be_queued_is_removed(tables, monkeypatch, error, status):
    ref, cmp = _completed_jobs(tables)

    async def publish(messages):
        raise BrokerUnavailable("down") if error == "broker" else pika.exceptions.UnroutableError([])
    monkeypatch.setattr(C, "publish_persistent", publish)
    request = ComparisonJobSubmitRequest(reference_job_id=ref.job_id, comparison_job_ids=[cmp.job_id])
    with pytest.raises(HTTPException) as e:
        asyncio.run(C.create_comparison_job(request, current_user=None))
    assert e.value.status_code == status and tables.ComparisonJob.select().count() == 0


def test_a_comparison_in_flight_for_too_long_is_not_reused(tables, monkeypatch):
    ref, cmp = _completed_jobs(tables)
    first = _save_comparison(tables, monkeypatch, ref, cmp)
    assert _save_comparison(tables, monkeypatch, ref, cmp).comparison_id == first.comparison_id
    tables.ComparisonJob.update(updated_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).execute()
    assert _save_comparison(tables, monkeypatch, ref, cmp).comparison_id != first.comparison_id


# --------------------------------------------------------------------------- #
# Monitoring and parameter bounds
# --------------------------------------------------------------------------- #

def test_queue_depths_include_the_retry_queues(monkeypatch):
    class Channel:
        def queue_declare(self, queue, passive):
            if queue == "comparison_jobs":
                raise pika.exceptions.ChannelClosedByBroker(404, "NOT_FOUND")
            return types.SimpleNamespace(method=types.SimpleNamespace(message_count=len(queue)))

        def close(self):
            pass
    monkeypatch.setattr(rabbitmq, "_short_connection", lambda timeout_s: types.SimpleNamespace(
        channel=Channel, close=lambda: None))
    depths = rabbitmq.queue_depths()
    assert depths["new"] == 3 and depths["new.retry"] == 9 and depths["comparison_jobs"] is None
    assert {q + ".retry" for q in rabbitmq.RETRIED_QUEUES} <= set(depths)


def test_health_does_not_return_exception_texts(monkeypatch):
    def down(*a, **k):
        raise RuntimeError("could not connect to 10.0.3.7:5672 as admin")
    monkeypatch.setattr(M, "rabbitmq_ping", down)
    monkeypatch.setattr(M, "Job", types.SimpleNamespace(select=lambda: types.SimpleNamespace(count=lambda: 0)))
    monkeypatch.setattr(M, "get_s3", lambda: types.SimpleNamespace(buckets=types.SimpleNamespace(all=lambda: [])))
    r = asyncio.run(M.health_check())
    assert r.services == {"database": "healthy", "rabbitmq": "unhealthy", "s3": "healthy"}


def _bounds(fn, name):
    meta = inspect.signature(fn).parameters[name].default.metadata
    return {k: getattr(m, k) for m in meta for k in ("ge", "gt", "le") if getattr(m, k, None) is not None}


def test_parameter_bounds():
    assert _bounds(J.submit_job, "num_epochs")["le"] == 2**31 - 1
    assert _bounds(V2.create_job, "num_epochs")["le"] == 2**31 - 1
    assert _bounds(V2.list_jobs, "size") == {"ge": 1, "le": 100}
    assert _bounds(V2.list_jobs, "page") == {"ge": 1}


def test_packages_of_epoch_zero_and_pagination(tables):
    job = tables.Job.create()
    for epoch in (0, 1):
        for n in (1, 2, 3):
            tables.Package.create(package_number=n, epoch_num=epoch, job=job, status="MERGED")
    assert [p.epoch_num for p in J.list_job_packages(job.job_id, epoch_num=0, limit=1000, offset=0,
                                                     current_user=None)] == [0, 0, 0]
    page = J.list_job_packages(job.job_id, epoch_num=None, limit=2, offset=3, current_user=None)
    assert [(p.epoch_num, p.package_number) for p in page] == [(1, 1), (1, 2)]
