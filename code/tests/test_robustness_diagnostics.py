# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Tests for the in-loop diagnostics of src/diagnostics.py: the
is_finite_state guard, the conserved quantities (energy, momentum) and the
centre-of-mass drift. CPU-only, no stack."""
from __future__ import annotations

import math

import numpy as np
import pytest

import diagnostics as diag
import physics


EPS2 = 1e-4


# ---------------------------------------------------------------------------
# Finite-state guard
# ---------------------------------------------------------------------------

def test_is_finite_state_true_for_clean_state():
    pos = np.random.default_rng(0).normal(size=(20, 3))
    vel = np.random.default_rng(1).normal(size=(20, 3))
    assert diag.is_finite_state(pos, vel) is True


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
@pytest.mark.parametrize("arr", ["pos", "vel"])
def test_is_finite_state_detects_nonfinite(bad, arr):
    pos = np.zeros((5, 3)); vel = np.zeros((5, 3))
    (pos if arr == "pos" else vel)[3, 1] = bad
    assert diag.is_finite_state(pos, vel) is False


# ---------------------------------------------------------------------------
# Conserved-quantity diagnostics
# ---------------------------------------------------------------------------

def test_energy_two_body_matches_closed_form():
    # two masses at rest, separation d -> KE=0, PE = -G m1 m2 / sqrt(d^2+eps^2)
    d = 2.0
    pos = np.array([[0.0, 0, 0], [d, 0, 0]])
    vel = np.zeros((2, 3))
    masses = np.array([1.0, 3.0])
    ke, pe = diag.total_energy(pos, vel, masses, softening_sq=EPS2, G=1.0)
    assert ke == pytest.approx(0.0, abs=1e-15)
    assert pe == pytest.approx(-1.0 * 1.0 * 3.0 / math.sqrt(d * d + EPS2), rel=1e-12)


def test_momentum_and_angular_momentum_known_values():
    pos = np.array([[1.0, 0, 0], [-1.0, 0, 0]])
    vel = np.array([[0.0, 1.0, 0], [0.0, -1.0, 0]])
    masses = np.array([2.0, 2.0])
    p = diag.total_momentum(vel, masses)
    np.testing.assert_allclose(p, [0, 0, 0], atol=1e-15)
    L = diag.total_angular_momentum(pos, vel, masses)
    # each body: m*(x*vy - y*vx) in z = 2*(1*1) and 2*((-1)*(-1)) = 2+2 = 4
    np.testing.assert_allclose(L, [0, 0, 4.0], atol=1e-12)


def test_energy_is_a_usable_conservation_diagnostic_over_a_run():
    """Velocity-verlet on a small system conserves total energy well — showing
    total_energy is meaningful as an in-loop diagnostic."""
    rng = np.random.default_rng(5)
    n = 12
    pos = rng.normal(size=(n, 3)); vel = rng.normal(scale=0.2, size=(n, 3))
    masses = np.full(n, 1.0 / n); ids = np.arange(n, dtype=np.int32)
    dt = 1e-3
    ke0, pe0 = diag.total_energy(pos, vel, masses, softening_sq=EPS2)
    e0 = ke0 + pe0
    a = physics.compute_accelerations(pos, masses, ids, softening_sq=EPS2)
    for _ in range(300):
        pos, vel, a = physics.step_velocity_verlet(pos, vel, masses, ids, a, dt, softening_sq=EPS2)
    ke1, pe1 = diag.total_energy(pos, vel, masses, softening_sq=EPS2)
    drift = abs((ke1 + pe1 - e0) / e0)
    assert drift < 1e-3, f"VV energy drift {drift:.2e} too large for a diagnostic"


# ---------------------------------------------------------------------------
# Compensated summation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Center of mass
# ---------------------------------------------------------------------------

def test_center_of_mass_mass_weighted():
    pos = np.array([[0.0, 0, 0], [1.0, 0, 0]])
    vel = np.array([[0.0, 0, 0], [0.0, 2.0, 0]])
    masses = np.array([3.0, 1.0])
    cp, cv = diag.center_of_mass(pos, vel, masses)
    np.testing.assert_allclose(cp, [0.25, 0, 0], atol=1e-15)
    np.testing.assert_allclose(cv, [0, 0.5, 0], atol=1e-15)


@pytest.mark.parametrize("n", [1, 2, 97, 1001])
def test_the_single_threaded_and_parallel_energies_are_the_same_bits(n):
    """The comparison service (potential_energy, one thread) and the workers'
    snapshot diagnostics (total_energy, parallel) report one energy."""
    rng = np.random.default_rng(n)
    pos, vel, m = rng.standard_normal((n, 3)), rng.standard_normal((n, 3)), rng.uniform(0.5, 1.5, n)
    ke, pe = diag.total_energy(pos, vel, m, 0.05, 1.0)
    assert ke == diag.kinetic_energy(vel, m)
    assert pe == diag.potential_energy(pos, m, 0.05, 1.0)


def test_the_comparison_energy_agrees_with_the_exact_one():
    """potential_energy_fast (fastmath) may reassociate its sums, nothing more."""
    rng = np.random.default_rng(5)
    pos, m = rng.standard_normal((2001, 3)), rng.uniform(0.5, 1.5, 2001)
    exact = diag.potential_energy(pos, m, 0.05, 1.0)
    assert diag.potential_energy_fast(pos, m, 0.05, 1.0) == pytest.approx(exact, rel=1e-12)
