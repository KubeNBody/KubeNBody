# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Predicted-background integrators for the distributed (package) steppers.

The distributed multi-stage integrators (velocity_verlet, rk4, forest_ruth)
compute forces on K package bodies using all N bodies as sources, but only the K
package bodies are actually integrated. The other N-K "background" bodies are
extrapolated to each intermediate stage time by a Taylor predictor whose order is
selected per step:

    0 frozen : x0                        -> global order 1  (a control)
    1 linear : x0 + tau*v                -> global order 2
    2 accel  : x0 + tau*v + 0.5*tau^2*a  -> global order 3   (RK4 / Forest-Ruth)

VV is intrinsically 2nd order, so it saturates at the linear predictor. The accel
term buys order only for RK4 and Forest-Ruth. (True 4th order would additionally
require a jerk-order predictor or per-stage inter-worker exchange — out of scope.)

Two properties are locked down:

1. K == N exact reduction (at EVERY predictor order): a single package covering
   ALL bodies reproduces the monolithic step bit-for-bit. The scatter overwrites
   every source row, so all predictor terms vanish and no approximation remains.

2. Convergence order matches the predictor: stepping an analytic two-body orbit
   through the REAL distributed pattern (both packages computed from the same
   snapshot, then merged) converges at order predictor_order + 1.

CPU-only. No GPU, no ORM. Runs in the standard suite.
"""
from __future__ import annotations

import numpy as np
import pytest

import barnes_hut
import physics


# ---------------------------------------------------------------------------
# 1. K == N exact reduction: package step over all bodies == monolithic step
# ---------------------------------------------------------------------------

def _cluster(n=6, seed=3):
    rng = np.random.default_rng(seed)
    pos = rng.normal(size=(n, 3))
    vel = rng.normal(scale=0.3, size=(n, 3))
    masses = np.full(n, 1.0 / n)
    ids = np.arange(n, dtype=np.int32)
    return pos, vel, masses, ids


DT = 1e-3
EPS2 = 1e-6
THETA = 0.5
G = 1.0


@pytest.mark.parametrize("predictor_order", [1, 2])
@pytest.mark.parametrize("method", ["velocity_verlet", "rk4", "forest_ruth"])
def test_knn_reduces_to_monolithic_direct(method, predictor_order):
    """Direct O(N^2): one package = all bodies must match the monolithic step,
    at every predictor order (the scatter overwrites all predictor terms)."""
    pos, vel, masses, ids = _cluster()
    a0 = physics.compute_accelerations(pos, masses, ids, softening_sq=EPS2)

    if method == "velocity_verlet":
        m_p, m_v, _ = physics.step_velocity_verlet(pos, vel, masses, ids, a0, DT, softening_sq=EPS2)
        ka0 = physics.compute_package_accelerations(pos, ids, pos, masses, ids, softening_sq=EPS2)
        p_p, p_v, _ = physics.step_package_velocity_verlet(
            pos, vel, masses, ids, ka0, pos, vel, a0, masses, ids, DT,
            predictor_order=predictor_order, softening_sq=EPS2)
    elif method == "rk4":
        m_p, m_v, _ = physics.step_rk4(pos, vel, masses, ids, DT, softening_sq=EPS2)
        p_p, p_v, _ = physics.step_package_rk4(
            pos, vel, masses, ids, pos, vel, a0, masses, ids, DT,
            predictor_order=predictor_order, softening_sq=EPS2)
    else:
        m_p, m_v, _ = physics.step_forest_ruth(pos, vel, masses, ids, DT, softening_sq=EPS2)
        p_p, p_v, _ = physics.step_package_forest_ruth(
            pos, vel, masses, ids, pos, vel, a0, masses, ids, DT,
            predictor_order=predictor_order, softening_sq=EPS2)

    np.testing.assert_array_equal(p_p, m_p,
                                  err_msg=f"{method}(pred{predictor_order}): K=N positions != monolithic")
    np.testing.assert_array_equal(p_v, m_v,
                                  err_msg=f"{method}(pred{predictor_order}): K=N velocities != monolithic")


@pytest.mark.parametrize("predictor_order", [1, 2])
@pytest.mark.parametrize("method", ["velocity_verlet", "rk4", "forest_ruth"])
def test_knn_reduces_to_monolithic_barnes_hut(method, predictor_order):
    """Barnes-Hut: one package = all bodies must match the monolithic BH step,
    at every predictor order."""
    pos, vel, masses, ids = _cluster()
    a0 = barnes_hut.compute_accelerations_barnes_hut(pos, masses, ids, THETA, G, EPS2)

    if method == "velocity_verlet":
        m_p, m_v, _ = physics.step_velocity_verlet(
            pos, vel, masses, ids, a0, DT, algorithm=physics.BARNES_HUT, theta=THETA, softening_sq=EPS2)
        ka0 = barnes_hut.compute_package_accelerations_barnes_hut(pos, ids, pos, masses, ids, THETA, G, EPS2)
        p_p, p_v, _ = physics.step_package_velocity_verlet(
            pos, vel, masses, ids, ka0, pos, vel, a0, masses, ids, DT,
            predictor_order=predictor_order, algorithm=physics.BARNES_HUT, theta=THETA, softening_sq=EPS2)
    elif method == "rk4":
        m_p, m_v, _ = physics.step_rk4(pos, vel, masses, ids, DT, algorithm=physics.BARNES_HUT, theta=THETA, softening_sq=EPS2)
        p_p, p_v, _ = physics.step_package_rk4(
            pos, vel, masses, ids, pos, vel, a0, masses, ids, DT,
            predictor_order=predictor_order, algorithm=physics.BARNES_HUT, theta=THETA, softening_sq=EPS2)
    else:
        m_p, m_v, _ = physics.step_forest_ruth(pos, vel, masses, ids, DT, algorithm=physics.BARNES_HUT, theta=THETA, softening_sq=EPS2)
        p_p, p_v, _ = physics.step_package_forest_ruth(
            pos, vel, masses, ids, pos, vel, a0, masses, ids, DT,
            predictor_order=predictor_order, algorithm=physics.BARNES_HUT, theta=THETA, softening_sq=EPS2)

    np.testing.assert_array_equal(p_p, m_p,
                                  err_msg=f"{method}(pred{predictor_order}): K=N BH positions != monolithic")
    np.testing.assert_array_equal(p_v, m_v,
                                  err_msg=f"{method}(pred{predictor_order}): K=N BH velocities != monolithic")


# ---------------------------------------------------------------------------
# 2. Convergence order through the real distributed (multi-package) pattern
# ---------------------------------------------------------------------------
# Equal-mass circular binary: G=1, m=1, r0=1, separation d=2 -> omega=0.5.
# Body 0 at (cos wt, sin wt, 0). Body 1 diametrically opposite. eps=0 so the
# analytic Kepler orbit is exact. Split into two single-body packages, so each
# body's entire force comes from the (predicted) background body -> the observed
# order is a clean readout of the predictor order.

def _binary_ic():
    pos = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
    vel = np.array([[0.0, 0.5, 0.0], [0.0, -0.5, 0.0]])
    masses = np.array([1.0, 1.0])
    ids = np.array([0, 1], dtype=np.int32)
    return pos, vel, masses, ids


def _analytic(t):
    w = 0.5
    c, s = np.cos(w * t), np.sin(w * t)
    return np.array([[c, s, 0.0], [-c, -s, 0.0]])


def _run_distributed(method, dt, t_final, predictor_order):
    """Step the binary as TWO single-body packages, both computed from the same
    epoch snapshot then merged — the actual distributed execution pattern.

    predictor_order: 0 frozen, 1 linear, 2 accel. The full-system epoch-start
    accelerations are recomputed each step and handed to the steppers (used only
    by the accel predictor).
    """
    pos, vel, masses, ids = _binary_ic()
    eps2 = 0.0
    nsteps = int(round(t_final / dt))
    for _ in range(nsteps):
        a_all = physics.compute_accelerations(pos, masses, ids, softening_sq=eps2)
        new_pos = np.empty_like(pos)
        new_vel = np.empty_like(vel)
        for k in (0, 1):
            kp, kv = pos[k:k + 1], vel[k:k + 1]
            km, kid = masses[k:k + 1], ids[k:k + 1]
            if method == "velocity_verlet":
                ka = physics.compute_package_accelerations(kp, kid, pos, masses, ids, softening_sq=eps2)
                p, v, _ = physics.step_package_velocity_verlet(
                    kp, kv, km, kid, ka, pos, vel, a_all, masses, ids, dt,
                    predictor_order=predictor_order, softening_sq=eps2)
            elif method == "rk4":
                p, v, _ = physics.step_package_rk4(
                    kp, kv, km, kid, pos, vel, a_all, masses, ids, dt,
                    predictor_order=predictor_order, softening_sq=eps2)
            else:
                p, v, _ = physics.step_package_forest_ruth(
                    kp, kv, km, kid, pos, vel, a_all, masses, ids, dt,
                    predictor_order=predictor_order, softening_sq=eps2)
            new_pos[k], new_vel[k] = p[0], v[0]
        pos, vel = new_pos, new_vel
    return pos


def _observed_order(method, predictor_order):
    t_final = 2.0
    dts = [0.04, 0.02, 0.01]
    errs = [np.linalg.norm(_run_distributed(method, dt, t_final, predictor_order) - _analytic(t_final))
            for dt in dts]
    # Richardson estimate from the two finest steps (least polluted by higher-order terms).
    return np.log2(errs[1] / errs[2]), errs


# (method, predictor_order, lo, hi): the predictor order dictates the observed
# global order = predictor_order + 1, capped by the integrator's own order
# (VV -> 2). This is the headline: order rises 1 -> 2 -> 3 as the background
# predictor improves frozen -> linear -> accel, with no per-stage communication.
_LADDER = [
    ("velocity_verlet", 0, 0.0, 1.3),   # gvv_frozen  -> ~1
    ("velocity_verlet", 1, 1.6, 2.4),   # gvv_linear  -> ~2 (saturated)
    ("rk4",             0, 0.0, 1.3),   # grk4_frozen -> ~1
    ("rk4",             1, 1.6, 2.4),   # grk4_linear -> ~2
    ("rk4",             2, 2.6, 3.6),   # grk4_accel  -> ~3
    ("forest_ruth",     0, 0.0, 1.3),   # gfr_frozen  -> ~1
    ("forest_ruth",     1, 1.6, 2.4),   # gfr_linear  -> ~2
    ("forest_ruth",     2, 2.6, 3.6),   # gfr_accel   -> ~3
]


@pytest.mark.parametrize("method,predictor_order,lo,hi", _LADDER)
def test_distributed_order_by_predictor(method, predictor_order, lo, hi):
    """The distributed convergence order tracks the background predictor order."""
    order, errs = _observed_order(method, predictor_order)
    assert lo < order < hi, (
        f"{method} predictor_order={predictor_order}: observed order {order:.2f} "
        f"not in ({lo}, {hi}), errs={errs}"
    )


@pytest.mark.parametrize("method", ["rk4", "forest_ruth"])
def test_accel_predictor_strictly_beats_linear(method):
    """The accel predictor is not just >2 in isolation — for RK4/FR it is a
    strict, order-of-magnitude accuracy improvement over the linear predictor at
    the same step size (extra evidence beyond the Richardson slope)."""
    err_linear = _run_distributed(method, 0.01, 2.0, predictor_order=1)
    err_accel = _run_distributed(method, 0.01, 2.0, predictor_order=2)
    e1 = np.linalg.norm(err_linear - _analytic(2.0))
    e2 = np.linalg.norm(err_accel - _analytic(2.0))
    assert e2 < e1 / 3.0, f"{method}: accel err {e2:.2e} not clearly better than linear {e1:.2e}"
