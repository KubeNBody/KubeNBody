# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Returned accelerations in the V2 pipeline.

For acceleration-predictor methods the merger assembles the next epoch's a(0)
from the accelerations the packages returned, instead of an O(N^2) recompute.
Checked end to end through DistributedWorkerV2 and a real redis-server:

  * gvv_accel reproduces the monolithic velocity Verlet run bitwise, with one
    force evaluation per task.
  * grk4_accel / gfr_accel reproduce the reference steppers in "returned" mode
    bitwise.
  * a merger that dies before or after shipping a(0) is taken over without
    changing the trajectory.
"""
from __future__ import annotations

import json

import numpy as np
import pytest
import reference_steppers as steppers

import task_timing
import v2.redis_service as rsmod
from helpers import (
    N_BODIES,
    N_EPOCHS,
    WorkerCrash,
    create_job,
    drive,
    initial_bodies,
    requires_redis,
    v2_worker,
)
from v2.binary_format import binary_to_numpy
from v2.orchestrator_v2 import OrchestratorV2


pytestmark = requires_redis

DT, EPS2, P = 0.01, 1e-2, 3


def _initial():
    b = initial_bodies()
    pos = np.array([[x["x"], x["y"], x["z"]] for x in b])
    vel = np.array([[x["vx"], x["vy"], x["vz"]] for x in b])
    m = np.array([x["m"] for x in b])
    ids = np.arange(1, N_BODIES + 1, dtype=np.int64)
    return pos, vel, m, ids


def _run_pipeline(svc, method):
    create_job(svc, method=method)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    p, v, _m, _ids = binary_to_numpy(finals["state"])
    return p, v, finals["state"]


def test_gvv_accel_pipeline_equals_monolithic_velocity_verlet(svc, no_pg):
    p, v, _ = _run_pipeline(svc, "gvv_accel")
    pos, vel, m, ids = _initial()
    pm, vm = steppers.run_monolithic("velocity_verlet", pos, vel, m, ids, DT, N_EPOCHS * DT, EPS2)
    assert np.array_equal(p, pm) and np.array_equal(v, vm)


@pytest.mark.parametrize("method,base", [("grk4_accel", "rk4"), ("gfr_accel", "forest_ruth")])
def test_pipeline_matches_harness(svc, no_pg, method, base):
    source = "returned"
    p, v, _ = _run_pipeline(svc, method)
    pos, vel, m, ids = _initial()
    ph, vh = steppers.run_distributed(base, pos, vel, m, ids, DT, N_EPOCHS * DT, EPS2,
                               n_packages=P, predictor_order=2, accel_source=source)
    assert np.array_equal(p, ph) and np.array_equal(v, vh)


@pytest.mark.parametrize("where", ["after_staged_commit", "after_publish_started"])
def test_takeover_keeps_returned_accelerations(svc, no_pg, monkeypatch, where):
    """A merger dies after committing an acceleration-predictor epoch. The
    takeover publishes it with the same a(0), which the commit installed from
    the staged returned rows."""
    _, _, reference = _run_pipeline(svc, "grk4_accel")
    svc.client.flushdb()

    create_job(svc, method="grk4_accel")
    w, backend, finals = v2_worker(svc)
    real_release = rsmod.RedisService.release_merge_lease
    hit = {"done": False}

    def die():
        hit["done"] = True
        monkeypatch.setattr(rsmod.RedisService, "release_merge_lease", lambda *a, **k: False)
        raise WorkerCrash(where)

    if where == "after_staged_commit":
        orig = rsmod.RedisService.commit_staged      # committed with a(0), nothing published

        def hook(self, job_id, generation, epoch, *args, **kwargs):
            result = orig(self, job_id, generation, epoch, *args, **kwargs)
            if not hit["done"] and epoch == 2:
                assert result[1], "the staged a(0) was not installed by the commit"
                die()
            return result

        monkeypatch.setattr(rsmod.RedisService, "commit_staged", hook)
    else:
        # installed by the commit, publication not yet run
        name = "publish_epoch"
        orig = getattr(rsmod.RedisService, name)

        def hook(self, job_id, generation, epoch, *args, **kwargs):
            if not hit["done"] and epoch == 2:
                die()
            return orig(self, job_id, generation, epoch, *args, **kwargs)

        monkeypatch.setattr(rsmod.RedisService, name, hook)

    drive(w, backend, svc,
           before_takeover=lambda: monkeypatch.setattr(rsmod.RedisService, "release_merge_lease", real_release))
    assert hit["done"]
    assert finals["state"] == reference


def test_tasks_after_epoch_one_read_the_shipped_accelerations(svc, no_pg, monkeypatch):
    """From epoch 2 on every task reads a(0) (N x 24 bytes) instead of
    recomputing it. For gvv_accel its rows are the package's cached
    acceleration, so the task needs one force evaluation."""
    monkeypatch.setattr(task_timing, "TIMING_ENABLED", True)
    _run_pipeline(svc, "gvv_accel")
    recs = [json.loads(x) for x in svc.client.lrange("v2:timings:1", 0, -1)]
    tasks = [r for r in recs if r["kind"] == "task"]
    assert all(r.get("bytes_accel_in", 0) == 0 for r in tasks if r["epoch"] == 1)
    assert all(r.get("bytes_accel_in") == N_BODIES * 24 for r in tasks if r["epoch"] > 1)


# The worker pipeline reproduces the reference steppers bitwise also with the
# linear predictor.
@pytest.mark.parametrize("method,base", [("grk4_linear", "rk4"), ("gfr_linear", "forest_ruth"),
                                         ("gvv_linear", "velocity_verlet")])
def test_pipeline_matches_harness_with_linear_predictor(svc, no_pg, method, base):
    p, v, _ = _run_pipeline(svc, method)
    pos, vel, m, ids = _initial()
    ph, vh = steppers.run_distributed(base, pos, vel, m, ids, DT, N_EPOCHS * DT, EPS2,
                               n_packages=P, predictor_order=1)
    assert np.array_equal(p, ph) and np.array_equal(v, vh)


def test_accel_source_is_recorded_per_job(svc, no_pg):
    """The job meta says which a(0) the epochs used."""
    source = "returned"
    _run_pipeline(svc, "grk4_accel")
    orch = OrchestratorV2.__new__(OrchestratorV2)
    orch.redis = svc
    sources = orch.get_job_status(1)["accel_sources"]
    assert set(sources) == {source} and sources[source] >= N_EPOCHS - 1
