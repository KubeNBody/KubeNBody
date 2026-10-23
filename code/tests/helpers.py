# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Shared test helpers (not fixtures: those live in conftest.py).

  * run_simulation: drive a stepper and record the energy history.
  * the V2 pipeline harness: a small job driven through the real
    DistributedWorkerV2 against the redis-server started by the `svc` fixture.
  * stubs for modules backed by PostgreSQL or RabbitMQ.
"""
import ast
import asyncio
import inspect
import shutil
import sys
import time
import types
from pathlib import Path

import numpy as np
import pytest

import v2.file_service_v2 as fs
import v2.orchestrator_v2 as orchmod
import workers.distributed_worker_v2 as dw
from diagnostics import kinetic_energy, potential_energy
from physics import compute_accelerations
from v2.orchestrator_v2 import OrchestratorV2
from v2.redis_service import TASKS_STREAM
from workers.backends.cpu_backend import CPUBackend
from workers.backends.redis_backend import RedisBackend
from workers.distributed_worker_v2 import DistributedWorkerV2


try:
    import redis
except ImportError:          # the Redis-backed tests are skipped then
    redis = None

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "code" / "src"

REDIS_SERVER = shutil.which("redis-server")
requires_redis = pytest.mark.skipif(redis is None or REDIS_SERVER is None,
                                    reason="redis-server not installed")


def run_simulation(step_fn, positions, velocities, masses, ids, dt, n_steps,
                   needs_accelerations=False):
    """Run a simulation for n_steps and return final state + energy history."""

    pos = positions.copy()
    vel = velocities.copy()
    accel = None

    energies = []

    for _ in range(n_steps):
        ke = kinetic_energy(vel, masses)
        pe = potential_energy(pos, masses)
        energies.append(ke + pe)

        if needs_accelerations:
            if accel is None:
                accel = compute_accelerations(pos, masses, ids)
            pos, vel, accel = step_fn(pos, vel, masses, ids, accel, dt)
        else:
            result = step_fn(pos, vel, masses, ids, dt)
            pos, vel = result[0], result[1]  # Works for both 2-tuple and 3-tuple returns

    ke = kinetic_energy(vel, masses)
    pe = potential_energy(pos, masses)
    energies.append(ke + pe)

    return pos, vel, np.array(energies)


def random_system(n=200, seed=2):
    """n equal-mass bodies with normal positions and small normal velocities."""
    rng = np.random.default_rng(seed)
    pos = rng.normal(size=(n, 3))
    vel = 0.1 * rng.normal(size=(n, 3))
    return pos, vel, np.full(n, 1.0 / n), np.arange(n, dtype=np.int64)


# --------------------------------------------------------------------------- #
# Module stubs
# --------------------------------------------------------------------------- #

def _enum(**members):
    return types.SimpleNamespace(**{k: types.SimpleNamespace(value=v) for k, v in members.items()})


JOB_STATUS = _enum(PENDING="PENDING", IN_PROGRESS="IN_PROGRESS", PAUSED="PAUSED", FAILED="FAILED",
                   CANCELLED="CANCELLED", COMPLETED="COMPLETED")
JOB_TYPE = _enum(MONOLITHIC="monolithic", DISTRIBUTED_V2="distributed_v2")


def import_watchdog(monkeypatch, Job, db, utcnow, monolithic_queue="m", gpu_queue="g", stub_schemas=True):
    """Import a fresh watchdog module over a stubbed `models` (the given Job
    table and clock) and `rabbitmq`. The caller pops sys.modules["watchdog"]
    again when done."""
    use_models(monkeypatch, types.SimpleNamespace(
        Job=Job, db=db, utcnow=utcnow, JobStatus=JOB_STATUS, JobType=JOB_TYPE))
    # the real module's other names stay (the monolithic worker, imported
    # under this stub by some tests, needs ConnectionThreadChannel & co.)
    import rabbitmq as real_rabbitmq
    monkeypatch.setitem(sys.modules, "rabbitmq", types.SimpleNamespace(**{
        **vars(real_rabbitmq), "get_rabbitmq_connection": None, "ensure_rabbitmq_queues": None,
        "MONOLITHIC_QUEUE": monolithic_queue, "GPU_MONOLITHIC_QUEUE": gpu_queue,
        "RABBITMQ_CONNECTION_ERRORS": ()}))
    if stub_schemas:
        monkeypatch.setitem(sys.modules, "schemas", types.SimpleNamespace(MonolithicJobMessage=object))
    sys.modules.pop("watchdog", None)
    import watchdog
    return watchdog


class SymbolicCondition(tuple):
    """A comparison of SymbolicColumn. Conditions combine with & like peewee's."""

    def __and__(self, other):
        return SymbolicCondition(("and", self, other))


class SymbolicColumn:
    """A peewee-like column: arithmetic and comparisons build tuples, so a
    fake model can record what an update would write."""

    def __init__(self, name):
        self.name = name

    def __add__(self, other):
        return ("add", self.name, other)

    def __eq__(self, other):
        return SymbolicCondition(("eq", self.name, other))

    def __ne__(self, other):
        return SymbolicCondition(("ne", self.name, other))

    def not_in(self, values):
        return SymbolicCondition(("not_in", self.name, tuple(values)))

    __hash__ = None


# --------------------------------------------------------------------------- #
# V2 pipeline harness (real DistributedWorkerV2, real redis-server)
# --------------------------------------------------------------------------- #

N_BODIES, PACKAGE_SIZE, N_EPOCHS = 24, 8, 4      # three packages
LEASE_MS = 300
ADAPTIVE = dict(adaptive_enabled=True, adaptive_eta=0.02, adaptive_dt_min=1e-6, adaptive_dt_max=0.05)


def fake_job_model(row, updates=None):
    """A stand-in for models.Job holding one row: get/get_or_none return it.
    Update records its fields in `updates`, or fails the test when `updates`
    is None (the code under test must not write PostgreSQL)."""
    class FakeJob:
        job_id = SymbolicColumn("job_id")
        status = SymbolicColumn("status")
        worker_id = SymbolicColumn("worker_id")

        class DoesNotExist(Exception):
            pass

        @staticmethod
        def get(*a, **k):
            return row

        get_or_none = get

        @staticmethod
        def update(**kw):
            if updates is None:
                raise AssertionError("PostgreSQL must not change here")
            updates.append(kw)
            return types.SimpleNamespace(where=lambda *a: types.SimpleNamespace(execute=lambda: 1))
    return FakeJob


def call_route(result):
    """A route handler's result: sync handlers return it, async ones a coroutine."""
    return asyncio.run(result) if inspect.iscoroutine(result) else result


class WorkerCrash(BaseException):
    """Simulates the merging worker dying (not caught by `except Exception`)."""


SRC = Path(__file__).resolve().parent.parent / "src"


def patch_where_used(monkeypatch, module, name, value, raising=True):
    """monkeypatch.setattr(module, name, value), and the same in every loaded
    src module that imported that object by name (`from module import name`
    binds its own reference, which a patch of the defining module misses)."""
    original = getattr(module, name, None)
    monkeypatch.setattr(module, name, value, raising=raising)
    # only real functions and classes: under the models stub every model name
    # is the same builtin `object`
    if not callable(original) or getattr(original, "__module__", None) == "builtins":
        return
    for mod in list(sys.modules.values()):
        path = getattr(mod, "__dict__", {}).get("__file__")    # stubs answer any getattr
        if mod is module or not isinstance(path, str) or not path.startswith(str(SRC)):
            continue
        for attr, bound in list(vars(mod).items()):
            if bound is original:
                monkeypatch.setattr(mod, attr, value)


_MODELS_NAMES = None


def _models_names():
    """{src module name: [names it binds with `from models import ...`]}."""
    global _MODELS_NAMES
    if _MODELS_NAMES is None:
        _MODELS_NAMES = {}
        for path in SRC.rglob("*.py"):
            mod = ".".join(path.relative_to(SRC).with_suffix("").parts).removesuffix(".__init__")
            names = [a.asname or a.name for n in ast.parse(path.read_text()).body
                     if isinstance(n, ast.ImportFrom) and n.module == "models" for a in n.names]
            if names:
                _MODELS_NAMES[mod] = names
    return _MODELS_NAMES


def use_models(monkeypatch, models):
    """Install a fake `models` module, and its names where the src modules
    imported them (`from models import Job` binds the module's own reference)."""
    monkeypatch.setitem(sys.modules, "models", models)
    for mod_name, names in _models_names().items():
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for name in names:
            if hasattr(models, name):
                monkeypatch.setattr(mod, name, getattr(models, name))


def set_meta(svc, job_id, **fields):
    base = dict(generation=0, committed_epoch=0, status="IN_PROGRESS")
    base.update(fields)
    svc.set_job_meta(job_id, base)


def add_task(svc, task, stream=TASKS_STREAM):
    """XADD one task (the pipeline enqueues whole epochs with enqueue_epoch_tasks)."""
    return svc.client.xadd(stream, task).decode()


def store_accelerations(svc, job_id, data):
    """Set a job's epoch-start accelerations a0 (the commit scripts write them)."""
    svc.client.setex(f"v2:accelerations:{job_id}", 3600, data)


def bump_generation(svc, job_id):
    """Start a new generation, as a recovery does. Returns the new value."""
    return svc.client.hincrby(f"v2:job:{job_id}:meta", "generation", 1)


def register(svc, job_id, gen, epoch, package_num, total, **kw):
    """register_partial of a one-body package (the job's state must hold
    `total` bodies)."""
    return svc.register_partial(job_id, gen, epoch, package_num, total, bytes(64), package_size=1, **kw)


def commit(svc, job_id, gen, epoch, state, next_dt, **kw):
    """Commit `state` for an epoch as a merger does: stage it as the epoch's
    only package, then commit_staged. Returns the commit code."""
    if svc.get_bodies(job_id) is None:
        svc.store_bodies(job_id, bytes(len(state)))
    svc.register_partial(job_id, gen, epoch, 1, 1, state, package_size=len(state) // 64)
    return svc.commit_staged(job_id, gen, epoch, 1, next_dt, **kw)[0]


def initial_bodies():
    rng = np.random.default_rng(11)
    pos = rng.normal(size=(N_BODIES, 3))
    vel = 0.1 * rng.normal(size=(N_BODIES, 3))
    return [
        dict(n=i + 1, idBody=i + 1, m=1.0 / N_BODIES,
             x=pos[i, 0], y=pos[i, 1], z=pos[i, 2], vx=vel[i, 0], vy=vel[i, 1], vz=vel[i, 2])
        for i in range(N_BODIES)
    ]


def v2_worker(svc):
    """A DistributedWorkerV2 on `svc`. Its finalization is the real one (lock,
    fence, finalize script, PostgreSQL record: the caller provides the `no_pg`
    fixture). Only the S3 upload of the final results is stubbed, and records
    the final state in `finals["state"]` (a test's own monkeypatched upload is
    still called)."""
    backend = RedisBackend()
    backend._redis = svc
    svc.ensure_consumer_group()
    w = DistributedWorkerV2(CPUBackend(), backend, worker_name="w-test")
    w._result_handler = w._handle_v2_results
    w.merge_lease_ms = LEASE_MS
    w.takeover_grace_s = 0.0
    finals = {}
    real_finalize = w._finalize_job

    def finalize(task, final_binary, redis_service):
        upload = dw.upload_final_results_csv

        def record(job_id, data, epoch):
            finals["state"] = data
            if upload.__module__ != fs.__name__:     # the test's own stub
                return upload(job_id, data, epoch)
            return f"v2/job_id/{job_id}/final_results.csv"

        dw.upload_final_results_csv = record
        try:
            return real_finalize(task, final_binary, redis_service)
        finally:
            dw.upload_final_results_csv = upload

    w._finalize_job = finalize

    # Every job snapshots its final epoch (interval 0 included): unless the
    # test stubbed the upload itself, keep snapshots in finals["snapshots"]
    # (epoch -> droppable) instead of reaching for S3.
    real_publish = w._publish_epoch

    def publish(*a, **k):
        upload = dw.upload_snapshot_async
        if upload.__module__ != fs.__name__:
            return real_publish(*a, **k)

        def record(job_id, epoch, data, droppable=False, **kw):
            finals.setdefault("snapshots", {})[epoch] = droppable
        dw.upload_snapshot_async = record
        try:
            return real_publish(*a, **k)
        finally:
            dw.upload_snapshot_async = upload

    w._publish_epoch = publish
    return w, backend, finals


def create_job(svc, job_id=1, method="rk4", snapshot_interval=0, n_bodies=None, package_size=None, **kw):
    """Create a V2 job of initial_bodies() (or its first n_bodies) through the
    orchestrator. Extra keyword arguments go to create_job_with_bodies."""
    orch = OrchestratorV2.__new__(OrchestratorV2)
    orch.redis = svc
    bodies = initial_bodies()
    if n_bodies is not None:
        bodies = bodies[:n_bodies]
    orch.create_job_with_bodies(
        job_id=job_id, bodies=bodies, num_epochs=N_EPOCHS, time_step=0.01,
        package_size=package_size or PACKAGE_SIZE, snapshot_interval=snapshot_interval,
        input_bucket="b", input_file_key="k", integration_method=method, softening_sq=1e-2, **kw,
    )
    return orch


def drive(worker, backend, svc, job_id=1, max_rounds=200, before_takeover=None):
    """Process tasks until the job completes. When the queue is empty, wait for
    leases to expire and let the worker take over stranded epochs."""
    for _ in range(max_rounds):
        if svc.get_job_meta(job_id).get("status") == "COMPLETED":
            return
        task = backend.claim_task("w-test", "q", block_ms=10)
        if task is None:
            time.sleep(LEASE_MS / 1000 + 0.05)
            if before_takeover:
                before_takeover()
            worker._try_takeover()
            continue
        try:
            ok = worker._process_task(task)
        except WorkerCrash:
            continue            # the task stays pending, like a dead worker's
        if ok:
            backend.ack_task(task)
    raise AssertionError("job did not complete")


def run_tasks(w, backend, n):
    """Claim, process and acknowledge n tasks. Return them."""
    out = []
    for _ in range(n):
        t = backend.claim_task("w-test", "q", block_ms=10)
        assert t is not None
        assert w._process_task(t)
        backend.ack_task(t)
        out.append(t)
    return out


def reference_run(svc, method="rk4"):
    """Final state of an uninterrupted run (Redis is flushed afterwards)."""
    create_job(svc, method=method)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    state = finals["state"]
    svc.client.flushdb()
    return state


class SnapshotStore(dict):
    """epoch -> (state, next_dt, a(0)). .times: epoch -> (simulation_time, dt_used)."""
    def __init__(self):
        super().__init__()
        self.times = {}


def capture_snapshots(monkeypatch):
    """Make snapshot uploads synchronous and keep them in memory. Point the
    orchestrator's recovery at them."""

    def upload(job_id, epoch, data, next_dt=None, accelerations=None, generation=None, is_current=None,
               simulation_time=None, dt_used=None, droppable=False):
        store[epoch] = (data, next_dt, accelerations)
        store.times[epoch] = (simulation_time, dt_used)
    store = SnapshotStore()
    patch_where_used(monkeypatch, fs, "upload_snapshot_async", upload)
    patch_where_used(monkeypatch, fs, "validate_binary_data", lambda data: True)
    monkeypatch.setattr(orchmod, "download_snapshot", lambda job_id, e: store[e][0])
    patch_where_used(monkeypatch, fs, "download_snapshot_aux", lambda job_id, e: fs.SnapshotAux(
        store[e][1], store[e][2], *store.times.get(e, (None, None))))
    return store
