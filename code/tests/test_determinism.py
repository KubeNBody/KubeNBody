# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Determinism and restart-reproducibility of the physics layer.

These pin down correctness properties that matter for a distributed, elastic,
fault-tolerant simulator:

  * run-to-run determinism (same inputs -> identical bytes),
  * worker-count invariance (splitting the bodies into more/fewer packages must
    not change the result — the "elastic scaling doesn't change physics" claim),
  * checkpoint/restore reproducibility (a crash-resumed run reproduces the
    uninterrupted run bit-for-bit).

Findings encoded here (see individual tests for the why):
  - monolithic integration is bitwise deterministic.
  - distributed EULER methods are bitwise worker-count invariant.
  - distributed MULTI-STAGE methods (VV/RK4/FR) are worker-count invariant only
    to O(dt^2): the drift-corrected background makes intra-package pairs exact
    but inter-package pairs ballistic, so the split boundary shifts truncation
    error by ~dt^2 (not a bug — a documented property of the scheme).
  - a single package over all bodies reduces to the monolithic step bitwise.
  - velocity-verlet restart is bitwise even when the acceleration is RECOMPUTED
    at resume, so positions+velocities+epoch+dt are sufficient restart state.
"""
from __future__ import annotations

import numpy as np
import pytest
import reference_steppers as steppers

import ic_generators as icg
import physics


DT = 1e-3
EPS2 = 1e-4
T_FINAL = 0.15


def _ic():
    return icg.plummer_sphere(30, seed=4)


def _key(pos, vel):
    return pos.tobytes() + vel.tobytes()


# ---------------------------------------------------------------------------
# Run-to-run determinism
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", steppers.METHODS)
def test_monolithic_run_to_run_bitwise(method):
    pos, vel, m, ids = _ic()
    r1 = steppers.run_monolithic(method, pos, vel, m, ids, DT, T_FINAL, EPS2)
    r2 = steppers.run_monolithic(method, pos, vel, m, ids, DT, T_FINAL, EPS2)
    assert _key(*r1) == _key(*r2), f"{method}: monolithic run is not bitwise reproducible"


# ---------------------------------------------------------------------------
# Worker-count invariance (elastic scaling)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["simple_euler", "symplectic_euler"])
def test_distributed_euler_is_bitwise_workercount_invariant(method):
    """Euler package steps compute each body's force by summing over all bodies
    in a fixed index order, independent of how bodies are partitioned — so the
    result is byte-identical for any number of workers."""
    pos, vel, m, ids = _ic()
    base = steppers.run_distributed(method, pos, vel, m, ids, DT, T_FINAL, EPS2, n_packages=1)
    for p in (2, 3, 7, 30):
        r = steppers.run_distributed(method, pos, vel, m, ids, DT, T_FINAL, EPS2, n_packages=p)
        assert _key(*r) == _key(*base), f"{method}: P={p} differs from P=1 (not worker-count invariant)"


@pytest.mark.parametrize("method", ["velocity_verlet", "rk4", "forest_ruth"])
def test_distributed_multistage_workercount_invariant_to_truncation(method):
    """Multi-stage package steps are worker-count invariant only to O(dt^2):
    intra-package pairs use exact stage positions, inter-package pairs use the
    ballistic drift, so shifting the partition boundary moves ~dt^2 of error.
    We assert the deviation is small and truncation-level (not a large bug)."""
    pos, vel, m, ids = _ic()
    base = steppers.run_distributed(method, pos, vel, m, ids, DT, T_FINAL, EPS2, n_packages=1)
    worst = 0.0
    for p in (2, 3, 7, 30):
        r = steppers.run_distributed(method, pos, vel, m, ids, DT, T_FINAL, EPS2, n_packages=p)
        worst = max(worst, float(np.max(np.abs(r[0] - base[0]))))
    assert worst < 1e-5, f"{method}: worker-count deviation {worst:.2e} exceeds truncation scale"


@pytest.mark.parametrize("method", ["velocity_verlet", "rk4", "forest_ruth"])
def test_single_package_equals_monolithic_bitwise(method):
    """One package over all bodies must reproduce the monolithic step byte-for-byte."""
    pos, vel, m, ids = _ic()
    mono = steppers.run_monolithic(method, pos, vel, m, ids, DT, T_FINAL, EPS2)
    dist = steppers.run_distributed(method, pos, vel, m, ids, DT, T_FINAL, EPS2, n_packages=1)
    assert _key(*mono) == _key(*dist), f"{method}: distributed K=N != monolithic bitwise"


# ---------------------------------------------------------------------------
# Checkpoint / restore reproducibility (reproducible restart)
# ---------------------------------------------------------------------------

def _serialize_restore(pos, vel):
    """Round-trip state through bytes, mimicking a snapshot write+read."""
    blob = np.concatenate([pos.ravel(), vel.ravel()]).tobytes()
    arr = np.frombuffer(blob, dtype=np.float64).reshape(2, -1, 3)
    return arr[0].copy(), arr[1].copy()


def test_velocity_verlet_restart_bitwise_with_recomputed_accel():
    """A VV run checkpointed at step K (persisting only positions+velocities)
    and resumed — recomputing the acceleration from positions — reproduces the
    uninterrupted run bit-for-bit. Confirms the accel need not be persisted."""
    pos, vel, m, ids = _ic()
    k_steps, total = 60, 120

    # uninterrupted
    p, v = pos.copy(), vel.copy()
    a = physics.compute_accelerations(p, m, ids, softening_sq=EPS2)
    for _ in range(total):
        p, v, a = physics.step_velocity_verlet(p, v, m, ids, a, DT, softening_sq=EPS2)
    uninterrupted = _key(p, v)

    # checkpoint at k_steps -> serialize -> resume (accel recomputed)
    p, v = pos.copy(), vel.copy()
    a = physics.compute_accelerations(p, m, ids, softening_sq=EPS2)
    for _ in range(k_steps):
        p, v, a = physics.step_velocity_verlet(p, v, m, ids, a, DT, softening_sq=EPS2)
    p, v = _serialize_restore(p, v)
    a = physics.compute_accelerations(p, m, ids, softening_sq=EPS2)
    for _ in range(total - k_steps):
        p, v, a = physics.step_velocity_verlet(p, v, m, ids, a, DT, softening_sq=EPS2)
    assert _key(p, v) == uninterrupted, "VV restart not bitwise reproducible"


@pytest.mark.parametrize("method", ["symplectic_euler", "velocity_verlet", "rk4", "forest_ruth"])
def test_distributed_restart_bitwise(method):
    """Distributed run checkpointed mid-way (persist positions+velocities) and
    resumed reproduces the uninterrupted distributed run bit-for-bit — the
    per-body stage state is fully recomputed from the snapshot each epoch."""
    pos, vel, m, ids = _ic()
    t_half = T_FINAL
    full = steppers.run_distributed(method, pos, vel, m, ids, DT, 2 * t_half, EPS2, n_packages=3)
    mid = steppers.run_distributed(method, pos, vel, m, ids, DT, t_half, EPS2, n_packages=3)
    p, v = _serialize_restore(mid[0], mid[1])
    resumed = steppers.run_distributed(method, p, v, m, ids, DT, t_half, EPS2, n_packages=3)
    assert _key(*resumed) == _key(*full), f"{method}: distributed restart not bitwise reproducible"
