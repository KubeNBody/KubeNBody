# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Tests for conservation of energy, momentum, and angular momentum."""
import numpy as np
import pytest

from constants import G_CONST
from diagnostics import kinetic_energy, potential_energy
from physics import (
    compute_accelerations,
    step_forest_ruth,
    step_rk4,
    step_simple_euler,
    step_symplectic_euler,
    step_velocity_verlet,
)
from services.comparison_service import compute_angular_momentum, compute_momentum


class TestMomentumConservation:
    """Total linear momentum should be conserved."""

    @pytest.mark.parametrize("integrator_name", [
        "symplectic_euler", "simple_euler", "velocity_verlet", "rk4", "forest_ruth"
    ])
    def test_momentum_conserved(self, kepler_circular, integrator_name):
        """Total momentum should be conserved for isolated system."""

        pos, vel, masses, ids = kepler_circular
        dt = 0.001
        n_steps = 1000

        step_fns = {
            "symplectic_euler": (step_symplectic_euler, False),
            "simple_euler": (step_simple_euler, False),
            "velocity_verlet": (step_velocity_verlet, True),
            "rk4": (step_rk4, False),
            "forest_ruth": (step_forest_ruth, False),
        }
        step_fn, needs_accel = step_fns[integrator_name]

        px0, py0, pz0 = compute_momentum(vel, masses)

        accel = None
        if needs_accel:
            accel = compute_accelerations(pos, masses, ids)

        for _ in range(n_steps):
            if needs_accel:
                pos, vel, accel = step_fn(pos, vel, masses, ids, accel, dt)
            else:
                result = step_fn(pos, vel, masses, ids, dt)
                pos, vel = result[0], result[1]

        px, py, pz = compute_momentum(vel, masses)
        dp = np.sqrt((px - px0)**2 + (py - py0)**2 + (pz - pz0)**2)

        assert dp < 1e-10, \
            f"{integrator_name}: momentum changed by {dp:.2e}"


class TestAngularMomentumConservation:
    """Angular momentum conserved for central force."""

    def test_angular_momentum_verlet(self, kepler_circular):
        """Verlet should conserve angular momentum well for Kepler orbit."""

        pos, vel, masses, ids = kepler_circular
        dt = 0.001
        n_steps = 6283

        lx0, ly0, lz0 = compute_angular_momentum(pos, vel, masses)
        L0 = np.sqrt(lx0**2 + ly0**2 + lz0**2)

        accel = compute_accelerations(pos, masses, ids)
        for _ in range(n_steps):
            pos, vel, accel = step_velocity_verlet(pos, vel, masses, ids, accel, dt)

        lx, ly, lz = compute_angular_momentum(pos, vel, masses)
        L = np.sqrt(lx**2 + ly**2 + lz**2)

        rel_error = abs(L - L0) / L0 if L0 > 1e-15 else abs(L - L0)
        assert rel_error < 1e-4, \
            f"Angular momentum changed by {rel_error:.2e}"

    def test_angular_momentum_rk4(self, kepler_circular):
        """RK4 should conserve angular momentum for Kepler orbit."""

        pos, vel, masses, ids = kepler_circular
        dt = 0.001
        n_steps = 6283

        lx0, ly0, lz0 = compute_angular_momentum(pos, vel, masses)
        L0 = np.sqrt(lx0**2 + ly0**2 + lz0**2)

        for _ in range(n_steps):
            pos, vel, _ = step_rk4(pos, vel, masses, ids, dt)

        lx, ly, lz = compute_angular_momentum(pos, vel, masses)
        L = np.sqrt(lx**2 + ly**2 + lz**2)

        rel_error = abs(L - L0) / L0 if L0 > 1e-15 else abs(L - L0)
        assert rel_error < 1e-6, \
            f"RK4 angular momentum changed by {rel_error:.2e}"


class TestEnergyComputation:
    """Test energy computation functions."""

    def test_kinetic_energy_formula(self):
        """KE = 0.5 * m * v^2."""

        velocities = np.array([[3.0, 4.0, 0.0]], dtype=np.float64)
        masses = np.array([2.0], dtype=np.float64)

        ke = kinetic_energy(velocities, masses)
        expected = 0.5 * 2.0 * (9.0 + 16.0)
        assert abs(ke - expected) < 1e-12, f"KE = {ke}, expected {expected}"

    def test_potential_energy_two_body(self):
        """PE = -G*m1*m2/r for two bodies."""

        positions = np.array([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0]], dtype=np.float64)
        masses = np.array([3.0, 5.0], dtype=np.float64)

        pe = potential_energy(positions, masses)
        expected = -G_CONST * 3.0 * 5.0 / 2.0
        assert abs(pe - expected) / abs(expected) < 1e-6, \
            f"PE = {pe}, expected {expected}"

    def test_virial_theorem_plummer(self, plummer_sphere_small):
        """Plummer sphere should approximately satisfy virial theorem: 2K ~ |W|."""

        pos, vel, masses, ids = plummer_sphere_small
        ke = kinetic_energy(vel, masses)
        pe = potential_energy(pos, masses)

        virial_ratio = 2 * ke / abs(pe)
        assert 0.3 < virial_ratio < 2.5, \
            f"Virial ratio Q = {virial_ratio:.2f}, expected near 1.0 (+-tolerance for N=50)"

    def test_total_energy_negative_bound_system(self, kepler_circular):
        """Bound Kepler orbit should have negative total energy."""

        pos, vel, masses, ids = kepler_circular
        E = kinetic_energy(vel, masses) + potential_energy(pos, masses)
        assert E < 0, f"Bound orbit should have E < 0, got E = {E}"


class TestMultiBodyConservation:
    """Conservation with N>2 bodies."""

    def test_plummer_energy_bounded_verlet(self, plummer_sphere_small):
        """Plummer sphere energy should be bounded with Verlet over 100 steps."""

        pos, vel, masses, ids = plummer_sphere_small
        dt = 0.001
        n_steps = 100

        E0 = (kinetic_energy(vel, masses) +
              potential_energy(pos, masses))

        accel = compute_accelerations(pos, masses, ids)
        for _ in range(n_steps):
            pos, vel, accel = step_velocity_verlet(pos, vel, masses, ids, accel, dt)

        E = (kinetic_energy(vel, masses) +
             potential_energy(pos, masses))

        rel_error = abs(E - E0) / abs(E0)
        assert rel_error < 1e-3, \
            f"Plummer energy drift: {rel_error:.2e} after {n_steps} steps"

    def test_plummer_momentum_conserved(self, plummer_sphere_small):
        """Multi-body momentum conservation."""

        pos, vel, masses, ids = plummer_sphere_small
        dt = 0.001
        n_steps = 100

        px0, py0, pz0 = compute_momentum(vel, masses)

        for _ in range(n_steps):
            pos, vel, _ = step_symplectic_euler(pos, vel, masses, ids, dt)

        px, py, pz = compute_momentum(vel, masses)
        dp = np.sqrt((px - px0)**2 + (py - py0)**2 + (pz - pz0)**2)

        assert dp < 1e-8, f"Plummer momentum change: {dp:.2e}"
