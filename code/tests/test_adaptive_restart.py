# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Adaptive time-stepping in the monolithic worker.

The Barnes-Hut branches of the CPU backend must return the accelerations of
explicit and symplectic Euler, RK4 and Forest-Ruth: with zeros the adaptive
step would be chosen from no force at all (always dt_max).

A restart of an adaptive job must continue from the checkpoint's step, not
from the initial one with no accelerations to choose the next step from:
snapshots of adaptive jobs carry the accelerations of the step that produced
them, so the resumed run is bitwise the uninterrupted one.

No database or object store: `models` is stubbed, and the snapshot uploader
and download_file share an in-memory store, with the real CSV writer and
checkpoint parser in between.
"""
from __future__ import annotations

import json
import logging
import sys
import types

import numpy as np
import pytest

import task_timing
import worker_metrics
import worker_monolithic_base as W
import workers.monolithic_worker as MW
from helpers import SymbolicColumn, patch_where_used, use_models
from workers.backends.cpu_backend import CPUBackend


BH_METHODS = ["simple_euler", "symplectic_euler", "rk4", "forest_ruth"]


def _cluster(n=12, seed=3):
    rng = np.random.default_rng(seed)
    pos = rng.normal(size=(n, 3))
    vel = 0.3 * rng.normal(size=(n, 3))
    return pos, vel, np.full(n, 1.0), np.arange(1, n + 1, dtype=np.int32)   # heavy: the adaptive step stays below dt_max


@pytest.mark.parametrize("method", BH_METHODS)
def test_barnes_hut_steps_return_their_accelerations(method):
    """The tree branches return the accelerations direct summation returns
    (start of step for the Euler methods, last stage for RK4 and Forest-Ruth),
    not zeros. With a tiny opening angle they agree with direct summation."""
    pos, vel, m, ids = _cluster()
    be = CPUBackend()
    kw = dict(positions=pos, velocities=vel, masses=m, ids=ids, time_step=1e-3,
              integration_method=method, softening_sq=1e-2)
    _, _, a_direct = be.compute_step(algorithm="direct", **kw)
    _, _, a_tree = be.compute_step(algorithm="barnes_hut", barnes_hut_theta=1e-6, **kw)
    assert np.abs(a_tree).max() > 0
    np.testing.assert_allclose(a_tree, a_direct, rtol=1e-9, atol=1e-12)


class _Store:
    """The object store: the snapshot uploader writes, download_file reads."""
    def __init__(self):
        self.objects = {}

    def submit(self, bucket, key, buffer):
        data = buffer.getvalue()
        self.objects[key] = data.decode() if isinstance(data, bytes) else data

    def drain(self):
        pass


def _run(monkeypatch, store, job, method, algorithm, start_epoch, bodies, num_epochs=12, interval=5):

    class Job:
        job_id = SymbolicColumn("job_id")
        worker_id = SymbolicColumn("worker_id")

        @staticmethod
        def update(**kw):
            return types.SimpleNamespace(where=lambda *a: types.SimpleNamespace(execute=lambda: 1))

        @staticmethod
        def get(*a, **k):
            return types.SimpleNamespace(status="IN_PROGRESS")
    use_models(monkeypatch, types.SimpleNamespace(Job=Job, utcnow=lambda: None))
    monkeypatch.setitem(sys.modules, "schemas", types.SimpleNamespace(
        JobStatus=types.SimpleNamespace(CANCELLED=types.SimpleNamespace(value="CANCELLED"),
                                        PAUSED=types.SimpleNamespace(value="PAUSED"))))
    for name in ("observe_epoch_batch",):
        monkeypatch.setattr(worker_metrics, name, lambda *a, **k: None)
    monkeypatch.setattr(MW, "renew_job_lease", lambda *a: True)
    patch_where_used(monkeypatch, W, "get_snapshot_uploader", lambda: store)
    w = MW.MonolithicWorker.__new__(MW.MonolithicWorker)
    w.compute = CPUBackend()
    w.worker_id = "w-test"
    w.shutdown_handler = types.SimpleNamespace(check_shutdown=lambda: False)
    config = MW.MonolithicJobConfig(
        job_id=job.job_id, bucket="b", file_key="k", num_epochs=num_epochs, time_step=1e-3,
        snapshot_interval=interval, algorithm=algorithm, barnes_hut_theta=0.5, softening_sq=1e-2,
        integration_method=method, adaptive_enabled=True, adaptive_eta=0.05,
        adaptive_dt_min=1e-6, adaptive_dt_max=2e-2)
    w._process_all_epochs(bodies, job, config, start_epoch)
    return config


def _bodies():
    pos, vel, m, ids = _cluster()
    return [dict(n=i, idBody=int(ids[i]), m=float(m[i]), x=pos[i, 0], y=pos[i, 1], z=pos[i, 2],
                 vx=vel[i, 0], vy=vel[i, 1], vz=vel[i, 2]) for i in range(len(m))]


def _final(store, job_id, epoch):
    return store.objects[f"job_id/{job_id}/epoch_num/{epoch}/epoch_results.csv"]


@pytest.mark.parametrize("method,algorithm", [("rk4", "direct"), ("forest_ruth", "direct"),
                                              ("symplectic_euler", "direct"), ("velocity_verlet", "direct"),
                                              ("rk4", "barnes_hut"), ("symplectic_euler", "barnes_hut")])
def test_adaptive_restart_continues_the_uninterrupted_run(monkeypatch, method, algorithm):
    """Through the real epoch loop and checkpoint loader, a run restarted
    from its epoch-10 checkpoint ends bitwise where the uninterrupted run
    ends, with the same step sizes."""
    full, resumed = _Store(), _Store()
    job = types.SimpleNamespace(job_id=41, current_epoch=0, current_simulation_time=0.0,
                                adaptive_enabled=True, status="IN_PROGRESS")
    _run(monkeypatch, full, job, method, algorithm, 1, _bodies())
    dts = [json.loads(full.objects[f"job_id/41/epoch_num/{e}/metadata.json"])["dt_used"] for e in (5, 10, 12)]
    assert len(set(dts)) > 1 and 1e-3 not in dts[1:]       # the step adapts, as it must for the test to mean anything

    # restart: the real resume path reads the epoch-10 checkpoint and sidecar
    resumed.objects = {k: v for k, v in full.objects.items() if "/epoch_num/12/" not in k and "/epoch_num/11/" not in k}
    monkeypatch.setattr(W, "download_file", lambda bucket, key: resumed.objects[key])
    monkeypatch.setattr(W, "validate_checkpoint_body_count", lambda *a: None)
    job2 = types.SimpleNamespace(job_id=41, current_epoch=11, current_simulation_time=-1.0,
                                 adaptive_enabled=True, status="IN_PROGRESS")
    message = types.SimpleNamespace(num_of_epochs=12, snapshot_interval=5, bucket="b", file_key="k")
    bodies, start = W.load_bodies_for_job(job2, message, True, logging.getLogger("t"))
    assert start == 11 and job2.resume_dt == dts[1]
    _run(monkeypatch, resumed, job2, method, algorithm, start, bodies)
    assert _final(resumed, 41, 12) == _final(full, 41, 12)
    assert resumed.objects["job_id/41/epoch_num/12/metadata.json"] == full.objects["job_id/41/epoch_num/12/metadata.json"]


def test_adaptive_restart_without_a_sidecar_still_runs(monkeypatch, caplog):
    """No sidecar (an older run): the restart falls back to the initial step,
    with a warning, rather than failing."""
    full = _Store()
    job = types.SimpleNamespace(job_id=42, current_epoch=0, current_simulation_time=0.0,
                                adaptive_enabled=True, status="IN_PROGRESS")
    _run(monkeypatch, full, job, "rk4", "direct", 1, _bodies())
    objects = {k: v for k, v in full.objects.items() if not k.endswith("metadata.json")}
    monkeypatch.setattr(W, "download_file", lambda bucket, key: objects[key])
    monkeypatch.setattr(W, "validate_checkpoint_body_count", lambda *a: None)
    job2 = types.SimpleNamespace(job_id=42, current_epoch=11, current_simulation_time=0.0,
                                 adaptive_enabled=True, status="IN_PROGRESS")
    message = types.SimpleNamespace(num_of_epochs=12, snapshot_interval=5, bucket="b", file_key="k")
    bodies, start = W.load_bodies_for_job(job2, message, True, logging.getLogger("t"))
    with caplog.at_level(logging.WARNING):
        _run(monkeypatch, _Store(), job2, "rk4", "direct", start, bodies)
    assert "without the checkpoint's step size" in caplog.text


@pytest.mark.parametrize("diagnostics", [False, True])
def test_the_per_snapshot_energy_pass_runs_only_with_the_diagnostics_flag(monkeypatch, diagnostics):
    """An O(N^2) pass per snapshot the distributed workers skip unless
    NBODY_SNAPSHOT_DIAGNOSTICS is on: timed runs of both engines do the same work."""
    calls = []
    real = MW.total_energy
    monkeypatch.setattr(MW, "total_energy", lambda *a: calls.append(1) or real(*a))
    monkeypatch.setattr(task_timing, "DIAGNOSTICS_ENABLED", diagnostics)
    job = types.SimpleNamespace(job_id=42, current_epoch=0, current_simulation_time=0.0,
                                adaptive_enabled=True, status="IN_PROGRESS")
    _run(monkeypatch, _Store(), job, "symplectic_euler", "direct", 1, _bodies())
    assert len(calls) == (3 if diagnostics else 0)       # snapshots at epochs 5, 10 and 12
