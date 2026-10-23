# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Monolithic jobs are acknowledged when claimed, owned through the lease.

Holding a job's RabbitMQ delivery unacknowledged until the job finishes lets
the broker's consumer_timeout (30 min by default) cut off and redeliver every
longer job (for example every 45-60 min RK4 or Forest-Ruth
direct-summation job of a 20,001-body model), and the watchdog would republish the
redelivered jobs waiting in the queue behind busy workers, adding duplicates.
So:

  - the worker acknowledges the message once it has claimed the job, and a
    LeaseRenewer keeps the lease fresh for the whole job.
  - a job handed back (graceful stop, retryable failure) gets an expired lease
    and is republished.
  - the watchdog republishes a job whose lease expired (dead owner) or whose
    owner made no progress for 30 min (hung), never a job waiting in the queue.

These tests use a real Peewee table (SQLite) with the real lease functions and
the real watchdog rules.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import peewee
import peewee as pw
import pika
import pytest

import workers.monolithic_worker as MW
from constants import LEASE_DURATION_SECONDS, MAX_REPUBLISH_ATTEMPTS
from helpers import import_watchdog, use_models


T0 = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A SQLite Job table wired into worker_monolithic_base and the watchdog."""
    db = pw.SqliteDatabase(str(tmp_path / "jobs.db"), check_same_thread=False)
    # timezone-aware values, as utcnow() returns them, read back aware
    tz_formats = ["%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z"]

    def Stamp():
        return pw.DateTimeField(null=True, formats=tz_formats)

    class Job(pw.Model):
        job_id = pw.IntegerField(primary_key=True)
        status = pw.CharField(default="IN_PROGRESS")
        job_type = pw.CharField(default="monolithic")
        worker_id = pw.CharField(null=True)
        leased_at = Stamp()
        handed_back_at = Stamp()
        updated_at = Stamp()
        created_at = Stamp()
        last_retry_at = Stamp()
        completed_at = Stamp()
        results_file_key = pw.CharField(null=True)
        current_epoch = pw.IntegerField(default=0)
        num_epochs = pw.IntegerField(default=1000)
        retry_count = pw.IntegerField(default=0)
        gpu_enabled = pw.BooleanField(default=False)
        input_bucket = pw.CharField(default="inputfiles")
        input_file_key = pw.CharField(default="files/2/galaxy4_4a.csv")
        time_step = pw.FloatField(default=1.25e-4)
        snapshot_interval = pw.IntegerField(default=50)
        force_algorithm = pw.CharField(default="direct")
        barnes_hut_theta = pw.FloatField(default=0.5)
        softening_sq = pw.FloatField(default=1.0)
        integration_method = pw.CharField(default="rk4")
        adaptive_enabled = pw.BooleanField(default=False)
        adaptive_eta = pw.FloatField(default=0.02)
        adaptive_dt_min = pw.FloatField(default=0.0)
        adaptive_dt_max = pw.FloatField(default=0.0)

        class Meta:
            database = db
    db.create_tables([Job])
    clock = _Clock()

    published = []
    wd = import_watchdog(monkeypatch, Job, db, clock, monolithic_queue="monolithic_queue",
                         gpu_queue="gpu_monolithic_queue", stub_schemas=False)
    # these tests cover monolithic jobs: the V2 checks would connect to a real
    # Redis on localhost (and wait out its connection retries when none runs)
    monkeypatch.setattr(wd, "detect_stalled_v2_jobs", lambda *a, **k: [])
    monkeypatch.setattr(wd, "reconcile_terminal_v2_jobs", lambda *a, **k: 0)
    import worker_monolithic_base as W
    monkeypatch.setattr(W, "Job", Job)
    monkeypatch.setattr(W, "utcnow", clock)
    channel = types.SimpleNamespace(basic_publish=lambda **k: published.append(k["routing_key"]))
    yield types.SimpleNamespace(Job=Job, db=db, clock=clock, wd=wd, W=W, published=published, channel=channel)
    sys.modules.pop("watchdog", None)
    db.close()


def _claimed(env, job_id=1, worker="pod-a-CPU", epoch=300):
    env.Job.create(job_id=job_id, created_at=env.clock(), updated_at=env.clock(), current_epoch=epoch)
    assert env.W.acquire_job_lease(job_id, worker)
    return env.Job.get_by_id(job_id)


def test_renewer_keeps_the_lease_fresh_and_reports_a_takeover(env):
    _claimed(env)
    calls = []

    def renew(job_id, worker_id):
        calls.append(worker_id)
        return env.W._renew_or_none(job_id, worker_id)
    r = env.W.LeaseRenewer(1, "pod-a-CPU", interval=0.01, renew=renew, db=env.db).start()
    env.clock.advance(minutes=5)
    for _ in range(200):
        if env.Job.get_by_id(1).leased_at == env.clock():
            break
        __import__("time").sleep(0.01)
    assert env.Job.get_by_id(1).leased_at == env.clock() and not r.lost
    env.Job.update(worker_id="pod-b-CPU").where(env.Job.job_id == 1).execute()     # a takeover
    r._thread.join(timeout=5)
    assert r.lost and not r._thread.is_alive()


def test_renewer_retries_when_the_database_is_unreachable(env):
    _claimed(env)
    answers = iter([None, None, True])
    r = env.W.LeaseRenewer(1, "pod-a-CPU", interval=0.01, renew=lambda *a: next(answers, True), db=env.db).start()
    __import__("time").sleep(0.1)
    r.stop()
    assert not r.lost


def test_a_dead_owner_is_republished_after_its_lease_expires_and_taken_over_at_once(env):
    _claimed(env)                                         # worker A claims, acks, then dies: no renewals
    env.clock.advance(seconds=LEASE_DURATION_SECONDS)
    assert env.wd.monolithic_stale_reason(env.Job.get_by_id(1)) is None       # within the grace period
    env.clock.advance(seconds=env.wd.LEASE_GRACE_SECONDS + 1)
    assert "expired" in env.wd.monolithic_stale_reason(env.Job.get_by_id(1))
    assert [j.job_id for j in env.wd.detect_stale_jobs()] == [1]
    assert env.wd.republish_job(env.Job.get_by_id(1), env.channel)
    assert env.published == ["monolithic_queue"]
    row = env.Job.get_by_id(1)
    assert row.worker_id is None and row.retry_count == 1
    assert env.W.acquire_job_lease(1, "pod-b-CPU")       # worker B takes the republished job


def test_republish_keeps_the_lease_of_a_worker_that_took_the_job_over(env):
    """The watchdog publishes first and releases the lease after. A consumer can
    take the expired lease over in between. Releasing by job_id alone cleared the
    new owner's lease, leaving a running job with no owner."""
    _claimed(env)                                         # worker A claims, then dies
    env.clock.advance(seconds=LEASE_DURATION_SECONDS + env.wd.LEASE_GRACE_SECONDS + 1)

    def publish_and_take_over(**k):                       # B consumes the message at once
        env.published.append(k["routing_key"])
        assert env.W.acquire_job_lease(1, "pod-b-CPU")

    ch = types.SimpleNamespace(basic_publish=publish_and_take_over)
    assert env.wd.republish_job(env.Job.get_by_id(1), ch)
    row = env.Job.get_by_id(1)
    assert row.worker_id == "pod-b-CPU" and row.leased_at is not None   # B keeps its lease
    assert row.retry_count == 1                                          # the attempt still counts
    assert env.W.renew_job_lease(1, "pod-b-CPU")

def test_a_job_waiting_in_the_queue_is_never_republished(env):
    """A handed-back or redelivered job waiting for a free worker has no owner
    and an old updated_at, yet must not be republished (it would run twice)."""
    env.Job.create(job_id=2, created_at=env.clock(), updated_at=env.clock(), current_epoch=400,
                   worker_id=None, leased_at=None)
    env.clock.advance(hours=2)
    assert env.wd.monolithic_stale_reason(env.Job.get_by_id(2)) is None
    assert env.wd.detect_stale_jobs() == []


def test_a_hung_owner_is_republished_and_a_live_one_is_not(env):
    _claimed(env)
    env.clock.advance(minutes=29)
    env.W.renew_job_lease(1, "pod-a-CPU")                # alive: lease renewed...
    assert env.wd.monolithic_stale_reason(env.Job.get_by_id(1)) is None
    env.clock.advance(minutes=2)                          # ...but no progress written for 31 min
    env.W.renew_job_lease(1, "pod-a-CPU")
    assert "no progress" in env.wd.monolithic_stale_reason(env.Job.get_by_id(1))
    env.Job.update(updated_at=env.clock()).where(env.Job.job_id == 1).execute()     # progress
    assert env.wd.monolithic_stale_reason(env.Job.get_by_id(1)) is None


def test_a_handed_back_job_is_claimable_at_once_and_the_watchdog_covers_a_failed_publish(env):
    _claimed(env)
    assert env.W.expire_job_lease(1, "pod-a-CPU")
    row = env.Job.get_by_id(1)
    assert row.worker_id == "pod-a-CPU"                   # still owned: never ownerless
    # the republished message: the next worker takes the expired lease at once
    assert env.W.acquire_job_lease(1, "pod-b-CPU")
    # had the publish failed instead, the watchdog sees the expired lease
    _claimed(env, job_id=3)
    env.W.expire_job_lease(3, "pod-a-CPU")
    assert "expired" in env.wd.monolithic_stale_reason(env.Job.get_by_id(3))


def test_a_delayed_hand_back_is_claimable_at_once_but_left_to_its_message(env):
    """While a job handed back after an outage waits in the retry queue the
    watchdog must not republish it too (counting a retry each time, which
    would fail the job in a long outage). A failed publish is still recovered."""
    _claimed(env)
    assert env.W.expire_job_lease(1, "pod-a-CPU", delayed=True)
    assert env.wd.monolithic_stale_reason(env.Job.get_by_id(1)) is None
    env.clock.advance(seconds=20)                         # the 15 s retry delay, with room to spare
    assert env.wd.monolithic_stale_reason(env.Job.get_by_id(1)) is None
    _claimed(env, job_id=3)
    env.W.expire_job_lease(3, "pod-a-CPU", delayed=True)
    assert env.W.acquire_job_lease(3, "pod-b-CPU")       # the delayed message's consumer
    _claimed(env, job_id=4)
    env.W.expire_job_lease(4, "pod-a-CPU", delayed=True)
    env.clock.advance(seconds=env.wd.LEASE_GRACE_SECONDS)  # the publish failed
    assert "expired" in env.wd.monolithic_stale_reason(env.Job.get_by_id(4))


def _updates_fail(env, monkeypatch):
    """The database drops after the job row was read: lease UPDATEs fail."""
    real = env.db.execute_sql

    def execute_sql(sql, *a, **k):
        if sql.lstrip().upper().startswith("UPDATE"):
            raise peewee.OperationalError("server closed the connection unexpectedly")
        return real(sql, *a, **k)
    monkeypatch.setattr(env.db, "execute_sql", execute_sql)


def test_acquire_raises_on_an_unreachable_database_but_answers_a_held_lease(env, monkeypatch):
    _claimed(env)
    assert env.W.acquire_job_lease(1, "pod-b-CPU") is False          # held by pod-a: still False
    _updates_fail(env, monkeypatch)
    with pytest.raises(peewee.OperationalError):
        env.W.acquire_job_lease(1, "pod-b-CPU")


def test_a_database_drop_at_the_claim_retries_the_message_later(env, monkeypatch):
    """Answering False here acked the message as a duplicate. The queued job
    (no owner) was then invisible to the watchdog: lost."""
    import rabbitmq
    import schemas
    import worker_metrics
    import workers.monolithic_worker as MW
    env.Job.create(job_id=1, created_at=env.clock(), updated_at=env.clock())     # queued, no owner
    use_models(monkeypatch, types.SimpleNamespace(Job=env.Job, utcnow=env.clock))
    msg = types.SimpleNamespace(job_id=1, num_of_epochs=1000, force_algorithm="direct", integration_method="rk4")
    monkeypatch.setattr(schemas.MonolithicJobMessage, "from_json", staticmethod(lambda body: msg))
    monkeypatch.setattr(worker_metrics, "set_worker_status", lambda *a, **k: None)
    monkeypatch.setattr(MW, "acquire_job_lease", env.W.acquire_job_lease)
    monkeypatch.setattr(MW, "release_job_lease", env.W.release_job_lease)
    _updates_fail(env, monkeypatch)
    w = MW.MonolithicWorker.__new__(MW.MonolithicWorker)
    w.compute = types.SimpleNamespace(name="cpu")
    w.worker_id, w._queue_name = "pod-b-CPU", "monolithic_queue"
    w.shutdown_handler = types.SimpleNamespace(current_job_id=None, check_shutdown=lambda: False)
    calls = []
    ch = types.SimpleNamespace(
        basic_publish=lambda **k: calls.append(("publish", k["routing_key"], k["properties"].expiration)),
        basic_ack=lambda **k: calls.append(("ack",)), basic_nack=lambda **k: calls.append(("nack",)),
        stop_consuming=lambda: None)
    w._handle_message(ch, types.SimpleNamespace(delivery_tag=1, redelivered=False), None, b"{}")
    assert calls == [("publish", "monolithic_queue.retry", str(rabbitmq.RETRY_DELAY_MS)), ("ack",)]
    row = env.Job.get_by_id(1)
    assert row.retry_count == 0 and row.status == "IN_PROGRESS"


def test_worker_hand_back_expires_its_lease_and_republishes(env, monkeypatch):
    """The worker's own path: _hand_back stops the renewal, expires the lease
    (keeping it), and publishes the job to the queue it consumes."""
    job = _claimed(env)
    w = MW.MonolithicWorker.__new__(MW.MonolithicWorker)
    w.worker_id, w._queue_name = "pod-a-CPU", "monolithic_queue"
    w._lease_renewer = env.W.LeaseRenewer(1, "pod-a-CPU", interval=3600, db=env.db).start()
    published = []
    ch = types.SimpleNamespace(basic_publish=lambda **k: published.append((k["routing_key"], k["body"])))
    w._hand_back(ch, job)
    assert w._lease_renewer is None
    row = env.Job.get_by_id(1)
    assert row.worker_id == "pod-a-CPU"
    assert env.wd.monolithic_stale_reason(row) is not None           # expired
    assert published and published[0][0] == "monolithic_queue" and '"job_id": 1' in published[0][1]


def test_a_handed_back_message_takes_over_its_holders_unexpired_lease(env):
    """A hand-back during a database outage cannot expire the lease. The job
    then idled until the lease expired and the watchdog republished it (11 min
    on the local stack). The handed-back message may take that lease, but not
    one its holder renewed since (running the job again) nor another's."""
    _claimed(env)                                          # pod-a renewed at T0
    env.clock.advance(seconds=5)
    marker = ("pod-a-CPU", env.clock())                   # pod-a hands back, lease unexpired
    env.clock.advance(seconds=20)
    assert env.W.acquire_job_lease(1, "pod-b-CPU") is False           # a plain message: no
    assert env.W.acquire_job_lease(1, "pod-b-CPU", marker)            # the handed-back one: yes
    assert env.Job.get_by_id(1).worker_id == "pod-b-CPU"
    assert env.W.acquire_job_lease(1, "pod-c-CPU", marker) is False   # pod-b's lease: not pod-a's
    # pod-a renewed after its hand-back (a watchdog republish it took): kept
    _claimed(env, job_id=3)
    marker = ("pod-a-CPU", env.clock())
    env.clock.advance(seconds=30)
    env.W.renew_job_lease(3, "pod-a-CPU")
    assert env.W.acquire_job_lease(3, "pod-b-CPU", marker) is False


def test_a_hand_back_during_a_database_outage_is_republished_and_taken_over(env, monkeypatch):
    import rabbitmq
    import workers.monolithic_worker as MW
    job = _claimed(env)
    w = MW.MonolithicWorker.__new__(MW.MonolithicWorker)
    w.worker_id, w._queue_name = "pod-a-CPU", "monolithic_queue"
    w._lease_renewer = None
    use_models(monkeypatch, types.SimpleNamespace(Job=env.Job, utcnow=env.clock))
    published = []
    ch = types.SimpleNamespace(basic_publish=lambda **k: published.append(k))
    real = env.db.execute_sql

    def down(sql, *a, **k):
        raise peewee.OperationalError("could not translate host name \"db\" to address")
    monkeypatch.setattr(env.db, "execute_sql", down)
    w._hand_back(ch, job, delayed=True)
    monkeypatch.setattr(env.db, "execute_sql", real)       # the database is back
    assert len(published) == 1 and published[0]["routing_key"] == "monolithic_queue.retry"
    assert '"job_id": 1' in published[0]["body"]
    row = env.Job.get_by_id(1)
    assert row.worker_id == "pod-a-CPU" and env.wd.monolithic_stale_reason(row) is None   # not expired
    env.clock.advance(seconds=15)                          # the retry delay
    marker = rabbitmq.handback_marker(published[0]["properties"])
    assert marker[0] == "pod-a-CPU"
    assert env.W.acquire_job_lease(1, "pod-b-CPU", marker)


def test_a_handed_back_job_waiting_in_a_busy_queue_is_republished_uncounted(env):
    """The watchdog republished a handed-back job whose message was still
    queued behind busy workers, counting a retry each time: a long outage or
    a rolling restart could fail a job that never failed. It still republishes
    (the hand-back's publish may have failed), but uncounted and never failing
    the job. A lease taken since is counted as before."""
    _claimed(env)
    env.Job.update(retry_count=3).where(env.Job.job_id == 1).execute()   # budget spent
    assert env.W.expire_job_lease(1, "pod-a-CPU", delayed=True)
    env.clock.advance(minutes=5)                          # queue busy: message not consumed
    stale = env.wd.detect_stale_jobs()
    assert [j.job_id for j in stale] == [1]
    assert env.wd.should_republish_job(stale[0])          # not failed at the cap
    assert env.wd.republish_job(stale[0], env.channel)
    row = env.Job.get_by_id(1)
    assert row.retry_count == 3 and row.status == "IN_PROGRESS"
    assert row.worker_id is None and row.handed_back_at is None
    # taken over: the marker is cleared, so the new owner's lapse is counted
    assert env.W.acquire_job_lease(1, "pod-b-CPU")
    env.Job.update(retry_count=0).where(env.Job.job_id == 1).execute()
    env.clock.advance(minutes=12)                          # pod-b died
    stale = env.wd.detect_stale_jobs()
    assert env.wd.should_republish_job(stale[0]) and env.wd.republish_job(stale[0], env.channel)
    assert env.Job.get_by_id(1).retry_count == 1


def test_a_closed_broker_channel_reaches_the_reconnect_loop(env):
    """After a broker restart the watchdog must not keep publishing on its
    closed channel (logging "will retry" and never reconnecting, so a crashed
    worker's job is never republished): the error propagates to the main
    loop, which reconnects."""
    _claimed(env)
    env.clock.advance(seconds=LEASE_DURATION_SECONDS + env.wd.LEASE_GRACE_SECONDS + 1)

    def closed(**k):
        raise pika.exceptions.ChannelWrongStateError("Channel is closed.")
    dead = types.SimpleNamespace(basic_publish=closed)
    with pytest.raises(pika.exceptions.ChannelWrongStateError):
        env.wd.watchdog_iteration(dead)
    row = env.Job.get_by_id(1)
    assert row.worker_id == "pod-a-CPU" and row.retry_count == 0      # nothing changed: retried after reconnect


def test_a_job_that_recovered_is_not_failed_for_its_past_retries(env):
    """The staleness re-check comes before the retry limit: a job that made
    progress since detection is not failed."""
    _claimed(env)
    env.Job.update(retry_count=MAX_REPUBLISH_ATTEMPTS).where(env.Job.job_id == 1).execute()
    assert not env.wd.should_republish_job(env.Job.get_by_id(1))
    assert env.Job.get_by_id(1).status == "IN_PROGRESS"


def test_the_retry_limit_does_not_overwrite_a_job_that_finished_meanwhile(env, monkeypatch):
    _claimed(env)
    env.Job.update(retry_count=MAX_REPUBLISH_ATTEMPTS).where(env.Job.job_id == 1).execute()
    env.clock.advance(seconds=LEASE_DURATION_SECONDS + env.wd.LEASE_GRACE_SECONDS + 1)
    stale = env.Job.get_by_id(1)                          # what the watchdog reads...
    env.Job.update(status="COMPLETED").where(env.Job.job_id == 1).execute()   # ...then it finishes
    with monkeypatch.context() as m:
        m.setattr(env.Job, "get", staticmethod(lambda *a, **k: stale))
        assert not env.wd.should_republish_job(stale)
    assert env.Job.get_by_id(1).status == "COMPLETED"


def test_finalize_keeps_a_cancel_that_arrived_during_the_combine(env):
    env.Job.create(job_id=5, status="CANCELLED", created_at=env.clock(), updated_at=env.clock())
    job = env.Job.get_by_id(5)
    message = types.SimpleNamespace(snapshot_interval=0, num_of_epochs=10)
    env.W.finalize_job(job, message)
    assert env.Job.get_by_id(5).status == "CANCELLED"
