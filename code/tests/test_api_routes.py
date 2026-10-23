# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The generic job routes (routes/jobs.py) and the health endpoint
(routes/monitoring.py), over fake models."""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import types

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from playhouse.pool import PooledSqliteDatabase

import api
import api_schemas
import auth
import db_threads
import publisher as P
import routes.jobs as J
import routes.monitoring as M
import utils.input_files as IF
import v1_epochs
import v2.api_v2 as V2
from db_threads import ClosingRoute
from helpers import SRC, SymbolicColumn, call_route, fake_job_model, patch_where_used
from rabbitmq import FINISHED_EPOCH_QUEUE, TO_MERGE_QUEUE
from routes.files import attachment_disposition
from schemas import PackageStatus


def test_api_exposes_recovery_count_and_lease_owner():
    fields = api_schemas.JobResponse.model_fields
    assert "recovery_count" in fields and "lease_owner" in fields
    # keyword arguments and dict entries of the routes' code (parsed, so
    # comments and quoting do not matter)
    tree = ast.parse((SRC / "routes" / "jobs.py").read_text())
    keywords = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.keyword)]
    entries = [f"{ast.unparse(k)}: {ast.unparse(v)}" for n in ast.walk(tree) if isinstance(n, ast.Dict)
               for k, v in zip(n.keys, n.values) if k is not None]
    # one response builder, used by the list, detail, description and name routes
    assert keywords.count("recovery_count=job.recovery_count") == 1
    calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert calls.count("_job_to_response") == 4
    assert "'recovery_count': job.recovery_count" in entries
    assert "'current_epoch': job.current_epoch" in entries      # /status carries the epoch


# --------------------------------------------------------------------------- #
# V2 jobs are handled by the V2 API only
# --------------------------------------------------------------------------- #

# The generic cancel/pause/resume routes change PostgreSQL only, so a V2 job
# "cancelled" there would keep running: they refuse V2 jobs before changing
# anything.


@pytest.mark.parametrize("route,action", [("cancel_job", "cancel"), ("pause_job", "pause"),
                                          ("resume_job", "resume")])
def test_generic_lifecycle_routes_refuse_v2_jobs(monkeypatch, route, action):
    row = types.SimpleNamespace(job_id=5, job_type="distributed_v2", status="IN_PROGRESS")
    monkeypatch.setattr(J, "Job", fake_job_model(row))         # PostgreSQL must not change
    with pytest.raises(HTTPException) as e:
        call_route(getattr(J, route)(5, current_user=None))
    assert e.value.status_code == 409 and f"/api/v2/jobs/5/{action}" in e.value.detail


def test_generic_permanent_delete_hands_v2_jobs_to_the_v2_cleanup(monkeypatch):
    """Deleting only the row and job_id/{id}/ would orphan a V2 job's Redis
    state and v2/ objects: the generic delete hands V2 jobs to the V2 cleanup."""
    calls = []

    FakeJob = fake_job_model(types.SimpleNamespace(
        job_id=5, job_type="distributed_v2", status="COMPLETED",
        delete_instance=lambda: calls.append("generic row delete")))

    def v2_delete(job_id, current_user=None):
        calls.append(("v2", job_id))
        return {"message": "v2 deleted"}
    monkeypatch.setattr(J, "Job", FakeJob)
    monkeypatch.setattr(J, "delete_files_by_prefix", lambda *a: calls.append(("generic s3", a)))
    patch_where_used(monkeypatch, V2, "delete_job_permanent", v2_delete)
    assert asyncio.run(J.delete_job_permanently(5, current_user=None)) == {"message": "v2 deleted"}
    assert calls == [("v2", 5)]


@pytest.mark.parametrize("failing", [None, "redis", "snapshots", "results"])
def test_a_v2_permanent_delete_keeps_the_row_while_data_is_left(monkeypatch, failing):
    """The row is the only handle on a V2 job's Redis state and objects:
    dropped only once they are gone, so a failed cleanup can be retried."""
    deleted = []
    row = types.SimpleNamespace(job_id=5, job_type="distributed_v2", status="COMPLETED",
                                delete_instance=lambda: deleted.append(5))
    monkeypatch.setattr(V2, "Job", fake_job_model(row))

    def step(name):
        def run(*a, **k):
            if failing == name:
                raise ConnectionError(f"{name} unreachable")
        return run
    monkeypatch.setattr(V2, "get_orchestrator", lambda: types.SimpleNamespace(cleanup_completed_job=step("redis")))
    monkeypatch.setattr(V2, "delete_snapshots", step("snapshots"))
    monkeypatch.setattr(V2, "get_s3_client", lambda: types.SimpleNamespace(delete_object=step("results")))
    if failing is None:
        assert V2.delete_job_permanent(5, current_user=None)["job_id"] == 5
        assert deleted == [5]
    else:
        with pytest.raises(HTTPException) as e:
            V2.delete_job_permanent(5, current_user=None)
        assert e.value.status_code == 503 and deleted == []

def test_generic_cancel_still_cancels_a_monolithic_job(monkeypatch):
    updates = []
    row = types.SimpleNamespace(job_id=5, job_type="monolithic", status="IN_PROGRESS")
    monkeypatch.setattr(J, "Job", fake_job_model(row, updates))
    out = J.cancel_job(5, current_user=None)
    assert "cancelled" in out["message"] and updates[0]["status"] == "CANCELLED"


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

# `'healthy' in 'unhealthy: ...'` is true: the check must not match substrings.


@pytest.mark.parametrize("down", [None, "database", "rabbitmq", "s3"])
def test_health_reports_a_dependency_that_is_down(monkeypatch, down):
    def fail_if(name, value=None):
        def f(*a, **k):
            if down == name:
                raise RuntimeError(f"{name} unreachable")
            return value
        return f
    monkeypatch.setattr(M, "Job", types.SimpleNamespace(select=lambda: types.SimpleNamespace(
        count=fail_if("database", 0))))
    monkeypatch.setattr(M, "rabbitmq_ping", fail_if("rabbitmq"))
    monkeypatch.setattr(M, "get_s3", fail_if("s3", types.SimpleNamespace(
        buckets=types.SimpleNamespace(all=lambda: []))))
    monkeypatch.setattr(M, "_services", (float("-inf"), {}))
    r = asyncio.run(M.health_check())
    assert r.status == ("healthy" if down is None else "unhealthy")
    if down:
        assert r.services[down].startswith("unhealthy")


def test_health_checks_the_dependencies_at_most_every_ttl(monkeypatch):
    """The endpoint is public: a flood of requests must not become a flood of
    database, broker and object store connections."""
    calls = []
    monkeypatch.setattr(M, "_check_services", lambda: calls.append(1) or {"database": "healthy"})
    monkeypatch.setattr(M, "_services", (float("-inf"), {}))
    for _ in range(5):
        assert asyncio.run(M.health_check()).status == "healthy"
    assert len(calls) == 1
    monkeypatch.setattr(M, "_services", (M.time.monotonic() - M.HEALTH_TTL_S, {}))
    asyncio.run(M.health_check())
    assert len(calls) == 2


def test_liveness_checks_no_dependency(monkeypatch):
    """The liveness probe must not depend on the broker: a probe that opens a
    RabbitMQ connection (minutes of retries) would get the API restarted in a
    broker outage."""

    def boom(*a, **k):
        raise AssertionError("liveness touched a dependency")
    monkeypatch.setattr(M, "rabbitmq_ping", boom)
    monkeypatch.setattr(M, "get_s3", boom)
    assert asyncio.run(M.liveness_check()) == {"status": "alive"}


# --------------------------------------------------------------------------- #
# Resuming a v1 distributed job
# --------------------------------------------------------------------------- #

def _v1_resume(monkeypatch, rows, next_time_step=None):
    """Run v1_epochs.queue_v1_epoch on a fake Package table holding `rows`
    (package_number -> status) for the next epoch. Return the published
    (queue, body) pairs and the package numbers created."""
    created = []

    class Row(types.SimpleNamespace):
        pass

    table = [Row(package_id=100 + n, package_number=n, status=st, file_key=f"k{n}") for n, st in rows.items()]

    class Query(list):
        def where(self, *a):
            return self

        def order_by(self, *a):
            return self

    class Package:
        job = epoch_num = package_id = SymbolicColumn("x")

        @staticmethod
        def select():
            return Query(table)

        @staticmethod
        def create(**kw):
            created.append(kw["package_number"])
            return Row(package_id=200 + kw["package_number"], **kw)

    monkeypatch.setattr(v1_epochs, "Package", Package)
    published = []
    channel = types.SimpleNamespace(basic_publish=lambda **k: published.append(
        (k["routing_key"], json.loads(k["body"]))))
    job = types.SimpleNamespace(job_id=9, current_epoch=4, num_epochs=10, num_packages=3,
                                results_file_key="r", input_file_key="i", input_bucket="b",
                                gpu_enabled=False, package_size=10, time_step=0.01,
                                force_algorithm="direct", barnes_hut_theta=0.5, softening_sq=1e-2,
                                integration_method="rk4", adaptive_enabled=False, adaptive_eta=None,
                                adaptive_dt_min=None, adaptive_dt_max=None, next_time_step=next_time_step)
    v1_epochs.queue_v1_epoch(job, channel)
    return published, created


def test_resume_reuses_the_epoch_packages_and_publishes_only_unfinished_ones(monkeypatch):
    """Resume reuses the epoch's package rows: a fresh set would make the
    merger's TO_MERGE count overshoot the package count, and the epoch would
    never merge."""
    published, created = _v1_resume(monkeypatch, {1: PackageStatus.TO_MERGE.value,
                                                  2: PackageStatus.IN_PROGRESS.value})
    assert created == [3]
    assert [(q, m["package_num"], m["epoch_num"]) for q, m in published] == [("new", 2, 5), ("new", 3, 5)]


@pytest.mark.parametrize("stored,expected", [(None, 0.01), (0.0037, 0.0037)])
def test_resume_continues_with_the_step_the_orchestrator_recorded(monkeypatch, stored, expected):
    """An adaptive job's step changes every epoch: a resume continues with the
    recorded one, not the base step."""
    published, _ = _v1_resume(monkeypatch, {1: PackageStatus.IN_PROGRESS.value}, next_time_step=stored)
    assert {m["time_step"] for _, m in published} == {expected}


def test_resume_triggers_the_merge_a_pause_skipped(monkeypatch):
    done = PackageStatus.TO_MERGE.value
    published, created = _v1_resume(monkeypatch, {1: done, 2: done, 3: done})
    assert created == []
    assert [(q, m["epoch_num"]) for q, m in published] == [(TO_MERGE_QUEUE, 5)]


def test_a_connection_dropped_while_idle_is_replaced_before_publishing(monkeypatch):
    """The API's BlockingConnection is serviced only when used: after the broker
    dropped it while idle, is_closed still said open and the next submit failed.
    The channel helper pumps the connection first and reconnects on failure."""

    class Dead:
        is_closed = False

        def process_data_events(self, time_limit=None):
            raise ConnectionResetError("broker closed the connection")
    fresh_channel = types.SimpleNamespace(is_closed=False, confirm_delivery=lambda: None)
    fresh = types.SimpleNamespace(is_closed=False, is_open=True, channel=lambda: fresh_channel,
                                  process_data_events=lambda time_limit=None: None)
    monkeypatch.setattr(P, "rabbitmq_connection", Dead())
    monkeypatch.setattr(P, "rabbitmq_channel", types.SimpleNamespace(is_closed=False))
    monkeypatch.setattr(P, "publisher_connection", lambda **kw: fresh)
    monkeypatch.setattr(P, "ensure_rabbitmq_queues", lambda ch: None)
    assert asyncio.run(asyncio.to_thread(P.run_on_rabbitmq_channel, lambda ch: ch)) is fresh_channel


def _submit_kwargs(**over):
    """Every Form parameter of routes.jobs.submit_job at its default value."""
    kw = {}
    for name, p in inspect.signature(J.submit_job).parameters.items():
        d = p.default
        kw[name] = getattr(d, "default", d) if d is not inspect.Parameter.empty else None
    kw.update(over)
    return kw


@pytest.mark.parametrize("bad", [dict(job_type="bogus"), dict(snapshot_interval=50, num_epochs=10),
                                 dict(force_algorithm="nope"), dict(job_type="distributed_v2"),
                                 dict(adaptive_dt_min=0.1, adaptive_dt_max=0.01)])
def test_submit_rejects_bad_parameters_before_storing_the_file(monkeypatch, bad):
    """Parameters were validated after the upload, so a rejected submission left
    a library file (and an InputFile row) behind."""

    def no_store(*a, **kw):
        raise AssertionError("the file was stored before the parameters were checked")
    monkeypatch.setattr(IF, "InputFile", types.SimpleNamespace(create=no_store, get_or_none=no_store))
    upload = types.SimpleNamespace(filename="x.csv", size=10, read=None)
    good = dict(file=upload, job_type="monolithic", num_epochs=10, snapshot_interval=1,
                force_algorithm="direct", integration_method="symplectic_euler", current_user=None)
    kw = _submit_kwargs(**{**good, **bad})
    with pytest.raises(HTTPException) as err:
        asyncio.run(J.submit_job(**kw))
    assert err.value.status_code == 400


@pytest.mark.parametrize("endpoint", [J.submit_job, V2.create_job])
def test_every_float_parameter_refuses_inf_and_nan(endpoint):
    floats = [(n, p.default) for n, p in inspect.signature(endpoint).parameters.items()
              if p.annotation in (float, "float")]
    assert floats and all(any(getattr(m, "allow_inf_nan", None) is False for m in d.metadata)
                          for _, d in floats), floats


@pytest.mark.parametrize("path", ["/api/jobs", "/api/v2/jobs"])
@pytest.mark.parametrize("value", ["inf", "-inf", "1e400", "nan"])
def test_a_non_finite_time_step_is_422(path, value):
    """An infinite step was stored, and every later job list failed to
    serialise it (500)."""
    api.app.dependency_overrides[auth.get_current_user] = lambda: None
    try:
        r = TestClient(api.app).post(path, data={"file_id": "1", "num_epochs": "10", "time_step": value,
                                                 "job_type": "monolithic"})
    finally:
        api.app.dependency_overrides.clear()
    assert r.status_code == 422 and "time_step" in r.text


def test_download_header_survives_non_latin_file_names():
    """A non-Latin file name is not Latin-1 encodable, so the header carries
    an ASCII fallback in filename= and the UTF-8 name (RFC 5987)."""
    h = attachment_disposition('gałąź "dysk".csv')
    h.encode("latin-1")                                   # a valid header value
    assert 'filename="ga___ _dysk_.csv"' in h and "filename*=UTF-8''ga%C5%82%C4%85%C5%BA" in h


def test_resume_at_the_last_epoch_re_triggers_completion(monkeypatch):
    """A job paused as its last epoch finished has nothing left to compute.
    The orchestrator completes it on a re-sent FINISHED_EPOCH (resume returned
    early and the job stayed IN_PROGRESS)."""
    published = []
    channel = types.SimpleNamespace(basic_publish=lambda **k: published.append(
        (k["routing_key"], json.loads(k["body"]))))
    job = types.SimpleNamespace(job_id=9, current_epoch=10, num_epochs=10, results_file_key="r")
    v1_epochs.queue_v1_epoch(job, channel)
    assert [(q, m["epoch_num"], m["file_key"]) for q, m in published] == [(FINISHED_EPOCH_QUEUE, 10, "r")]


# --------------------------------------------------------------------------- #
# Pooled connections of threadpool handlers
# --------------------------------------------------------------------------- #

def test_sync_handlers_return_their_pooled_connection(monkeypatch, tmp_path):
    """Sync handlers run in the threadpool. The pool tracks connections per
    thread and reclaims none, so a handler that kept its connection leaked it
    when the thread was retired, and after max_connections every query failed."""
    db = PooledSqliteDatabase(str(tmp_path / "pool.db"), max_connections=2, timeout=1)
    monkeypatch.setattr(db_threads, "db", db)
    router = APIRouter(route_class=ClosingRoute)

    @router.get("/q")
    def query():
        return {"one": db.execute_sql("SELECT 1").fetchone()[0]}
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/q").json() == {"one": 1}
    assert len(db._in_use) == 0
