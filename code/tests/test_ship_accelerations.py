# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Shipping epoch-start accelerations a0 from the merger.

Instead of every worker recomputing a0 over all N bodies (an O(N^2) pass each),
the merger computes it once for the merged state and ships it as a separate flat
array parallel to the body state. These tests check that the serialization
round-trips exactly and rejects stale payloads, and when the merger ships.

Mostly CPU-only: the pure serialization and physics functions the live V2
path composes. One test (requires_redis) checks the commit's drop of a stale
payload against a real redis-server.
"""
from __future__ import annotations

import numpy as np
import pytest

import physics
import workers.distributed_worker_v2 as dw
from helpers import commit, create_job, requires_redis, store_accelerations
from integrators import PREDICTOR_ACCEL, IntegrationMethod, get_predictor_order, resolve_base_method
from v2.binary_format import (
    ACCEL_SIZE,
    accelerations_to_binary,
    binary_to_accelerations,
    numpy_to_binary,
)
from workers.backends.cpu_backend import CPUBackend


EPS2 = 1e-6


def _cluster(n=40, seed=7):
    rng = np.random.default_rng(seed)
    pos = rng.normal(size=(n, 3))
    vel = rng.normal(scale=0.3, size=(n, 3))
    masses = np.full(n, 1.0 / n)
    ids = np.arange(n, dtype=np.int32)
    return pos, vel, masses, ids


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #

def test_accel_binary_roundtrip_is_exact():
    pos, vel, masses, ids = _cluster()
    a0 = physics.compute_accelerations(pos, masses, ids, softening_sq=EPS2)
    blob = accelerations_to_binary(a0)
    assert len(blob) == len(masses) * ACCEL_SIZE
    back = binary_to_accelerations(blob, len(masses))
    assert np.array_equal(back, a0), "float64 LE round-trip must be bitwise exact"


def test_binary_to_accelerations_rejects_size_mismatch():
    pos, vel, masses, ids = _cluster(n=10)
    blob = accelerations_to_binary(
        physics.compute_accelerations(pos, masses, ids, softening_sq=EPS2))
    with pytest.raises(ValueError):
        binary_to_accelerations(blob, 11)   # stale/mismatched -> caller recomputes


def test_accelerations_to_binary_requires_n_by_3():
    with pytest.raises(ValueError):
        accelerations_to_binary(np.zeros((5, 2)))


# --------------------------------------------------------------------------- #
# Merger gating: only ship when the method actually needs a0 (predictor >= 2)
# --------------------------------------------------------------------------- #

def test_merger_ships_only_for_accel_predictor():
    """_maybe_ship_accelerations writes the accel key for grk4_accel but not for
    grk4_linear, and the written blob decodes to the correct a0."""

    class _Task:
        def __init__(self, method):
            self.integration_method = method
            self.algorithm = "direct"
            self.barnes_hut_theta = 0.5
            self.softening_sq = EPS2
            self.job_id = 1
            self.epoch_num = 3
            self.total_packages = 2
            self.generation = 0

    class _Redis:
        # a(0) not installed by the commit -> the exact O(N^2) path
        def __init__(self):
            self.stored = None
        def has_accelerations(self, job_id):
            return False
        def get_fence_state(self, job_id):
            return {"generation": "0", "committed_epoch": "3", "status": "IN_PROGRESS"}
        def ship_accelerations(self, job_id, generation, epoch, data, source=""):
            self.stored = data
            return 1

    pos, vel, masses, ids = _cluster()
    merged = numpy_to_binary(pos, vel, masses, ids)
    worker = dw.DistributedWorkerV2.__new__(dw.DistributedWorkerV2)  # no __init__ needed

    r_linear = _Redis()
    worker._maybe_ship_accelerations(_Task("grk4_linear"), merged, r_linear)
    assert r_linear.stored is None, "linear predictor must not ship a0"

    r_accel = _Redis()
    worker._maybe_ship_accelerations(_Task("grk4_accel"), merged, r_accel)
    assert r_accel.stored is not None, "accel predictor must ship a0"
    shipped = binary_to_accelerations(r_accel.stored, len(masses))
    expected = physics.compute_accelerations(pos, masses, ids, softening_sq=EPS2)
    np.testing.assert_array_equal(shipped, expected)


# --------------------------------------------------------------------------- #
# gvv_accel: velocity Verlet with the acceleration predictor
# --------------------------------------------------------------------------- #

def test_gvv_accel_is_a_valid_accel_method():
    assert IntegrationMethod.is_valid("gvv_accel")
    assert get_predictor_order("gvv_accel") == PREDICTOR_ACCEL
    assert resolve_base_method("gvv_accel") == "velocity_verlet"


def test_gvv_accel_backend_step_equals_monolithic_step():
    """The CPU backend's gvv_accel package step, fed the shipped a0, reproduces
    the monolithic velocity Verlet step bitwise for every package, and needs no
    local a0 evaluation (the package rows of the shipped a0 are its cache)."""
    pos, vel, masses, ids = _cluster()
    dt = 1e-3
    a0 = physics.compute_accelerations(pos, masses, ids, softening_sq=EPS2)
    mono_p, mono_v, _ = physics.step_velocity_verlet(pos, vel, masses, ids, a0, dt, softening_sq=EPS2)

    backend = CPUBackend()
    for sl in (slice(0, 13), slice(13, 27), slice(27, 40)):
        p, v, _ = backend.compute_package_step(
            pos[sl].copy(), vel[sl].copy(), masses[sl].copy(), ids[sl].copy(),
            pos, vel, masses, ids, dt,
            integration_method="gvv_accel", softening_sq=EPS2,
            all_accelerations=a0,
        )
        np.testing.assert_array_equal(p, mono_p[sl])
        np.testing.assert_array_equal(v, mono_v[sl])


@requires_redis
def test_commit_drops_stale_accelerations_and_failed_shipping_keeps_newer_ones(svc, monkeypatch):
    """A stale a(0) payload of the same size would pass the reader's size
    check. It cannot reach the next epoch: the commit deletes it in the same
    script that installs the new state (real Redis). So a publisher whose
    shipping fails has nothing to invalidate, and must not delete what a newer
    state shipped meanwhile."""
    create_job(svc, method="grk4_accel")
    store_accelerations(svc, 1, b"a(0) of epoch 0")
    assert commit(svc, 1, 0, 1, svc.get_bodies(1), 0.01) == 1
    assert not svc.has_accelerations(1)                   # the commit dropped it

    def read_fails(job_id):
        store_accelerations(svc, 1, b"a(0) of epoch 2")    # a newer state's, shipped meanwhile
        raise ConnectionError("read failed")
    monkeypatch.setattr(svc, "get_fence_state", read_fails)
    task = type("T", (), dict(integration_method="grk4_accel", algorithm="direct", barnes_hut_theta=0.5,
                              softening_sq=EPS2, job_id=1, epoch_num=1, total_packages=2, generation=0))()
    worker = dw.DistributedWorkerV2.__new__(dw.DistributedWorkerV2)
    worker._maybe_ship_accelerations(task, svc.get_bodies(1), svc)   # does not raise
    assert svc.get_accelerations(1) == b"a(0) of epoch 2"
