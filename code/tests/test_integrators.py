# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Tests for integration methods: accuracy, convergence order, and symplecticity."""
import numpy as np
import pytest

from diagnostics import kinetic_energy, potential_energy
from helpers import run_simulation
from physics import (
    BARNES_HUT,
    compute_accelerations,
    step_direct,
    step_forest_ruth,
    step_rk4,
    step_simple_euler,
    step_symplectic_euler,
    step_velocity_verlet,
)


class TestCircularOrbit:
    """All integrators should maintain a circular orbit for one period."""

    @pytest.mark.parametrize("integrator_name,expected_rel_error", [
        ("symplectic_euler", 1e-2),
        ("simple_euler", 5e-2),
        ("velocity_verlet", 1e-4),
        ("rk4", 1e-6),
        ("forest_ruth", 1e-6),
    ])
    def test_energy_conservation(self, kepler_circular, integrator_name, expected_rel_error):
        """Energy should be conserved within integrator's expected accuracy."""

        pos, vel, masses, ids = kepler_circular
        dt = 0.001
        n_steps = 6283  # ~1 orbital period (T = 2*pi)

        step_fns = {
            "symplectic_euler": (step_symplectic_euler, False),
            "simple_euler": (step_simple_euler, False),
            "velocity_verlet": (step_velocity_verlet, True),
            "rk4": (step_rk4, False),
            "forest_ruth": (step_forest_ruth, False),
        }
        step_fn, needs_accel = step_fns[integrator_name]

        _, _, energies = run_simulation(step_fn, pos, vel, masses, ids, dt, n_steps,
                                        needs_accelerations=needs_accel)

        E0 = energies[0]
        max_rel_error = np.max(np.abs((energies - E0) / abs(E0)))

        assert max_rel_error < expected_rel_error, \
            f"{integrator_name}: max relative energy error {max_rel_error:.2e} " \
            f"exceeds bound {expected_rel_error:.2e}"

    def test_circular_orbit_stays_circular(self, kepler_circular):
        """Radius should remain approximately constant with Verlet."""

        pos, vel, masses, ids = kepler_circular
        dt = 0.001
        n_steps = 6283

        accel = compute_accelerations(pos, masses, ids)
        radii = []

        for _ in range(n_steps):
            r = np.linalg.norm(pos[1] - pos[0])
            radii.append(r)
            pos, vel, accel = step_velocity_verlet(pos, vel, masses, ids, accel, dt)

        radii = np.array(radii)
        r0 = radii[0]
        max_deviation = np.max(np.abs(radii - r0)) / r0

        assert max_deviation < 1e-3, \
            f"Circular orbit radius deviated by {max_deviation:.2e} (>0.1%)"


class TestConvergenceOrder:
    """Verify each integrator achieves its theoretical convergence order.

    Run the same problem at dt, dt/2 and dt/4 and compare the final states.
    """

    @pytest.mark.parametrize("integrator_name,expected_order,base_dt", [
        ("symplectic_euler", 1, 0.0025),
        ("simple_euler", 1, 0.0025),
        ("velocity_verlet", 2, 0.01),
        ("rk4", 4, 0.01),
        ("forest_ruth", 4, 0.01),
    ])
    def test_convergence_order(self, kepler_circular, integrator_name, expected_order, base_dt):
        """Self-convergence of the final positions at t = 6: the difference
        between successive halvings of dt shrinks by 2^p for order p."""

        pos, vel, masses, ids = kepler_circular

        step_fns = {
            "symplectic_euler": (step_symplectic_euler, False),
            "simple_euler": (step_simple_euler, False),
            "velocity_verlet": (step_velocity_verlet, True),
            "rk4": (step_rk4, False),
            "forest_ruth": (step_forest_ruth, False),
        }
        step_fn, needs_accel = step_fns[integrator_name]

        finals = []
        for refinement in range(3):
            dt = base_dt / 2 ** refinement
            final_pos, _, _ = run_simulation(
                step_fn, pos.copy(), vel.copy(), masses, ids, dt, round(6.0 / dt),
                needs_accelerations=needs_accel
            )
            finals.append(final_pos)

        coarse = np.max(np.abs(finals[0] - finals[1]))
        fine = np.max(np.abs(finals[1] - finals[2]))
        measured_order = np.log2(coarse / fine)
        assert abs(measured_order - expected_order) < 0.2, \
            f"{integrator_name}: measured order {measured_order:.2f}, expected {expected_order}"


class TestSymplecticity:
    """At a fixed dt the symplectic methods' energy error oscillates without
    growing, while RK4 and explicit Euler drift. Measured on the e = 0.5 orbit
    (where Forest-Ruth's error stays well above round-off) as the largest
    relative energy error over the last two of 20 periods against the first two.
    Explicit Euler runs at a smaller dt: at 0.01 its orbit unbinds early and the
    error saturates, which caps the ratio near the threshold."""

    @pytest.mark.parametrize("integrator_name,step_fn,needs_accel,dt,bounded", [
        ("symplectic_euler", step_symplectic_euler, False, 0.01, True),
        ("velocity_verlet", step_velocity_verlet, True, 0.01, True),
        ("forest_ruth", step_forest_ruth, False, 0.01, True),
        ("rk4", step_rk4, False, 0.01, False),
        ("simple_euler", step_simple_euler, False, 0.005, False),
    ])
    def test_energy_error_bounded_only_for_symplectic_methods(self, kepler_eccentric, integrator_name,
                                                              step_fn, needs_accel, dt, bounded):
        pos, vel, masses, ids = kepler_eccentric
        period = 2 * np.pi
        n_steps = int(round(20 * period / dt))
        window = int(round(2 * period / dt))

        _, _, energies = run_simulation(step_fn, pos, vel, masses, ids, dt, n_steps,
                                        needs_accelerations=needs_accel)

        rel = np.abs((energies - energies[0]) / energies[0])
        growth = rel[-window:].max() / rel[:window].max()
        if bounded:
            assert growth < 1.1, f"{integrator_name}: energy error grew {growth:.2f}x over 20 periods"
        else:
            assert growth > 2, f"{integrator_name}: energy error grew only {growth:.2f}x, expected a drift"

    def test_forest_ruth_bounded_energy_barnes_hut(self, kepler_circular):
        """Forest-Ruth with Barnes-Hut: energy error should be bounded."""

        pos, vel, masses, ids = kepler_circular
        dt = 0.005
        n_steps = 6283  # ~5 periods (T = 2*pi)

        E0 = kinetic_energy(vel, masses) + potential_energy(pos, masses)

        for _ in range(n_steps):
            pos, vel, _ = step_forest_ruth(pos, vel, masses, ids, dt, algorithm=BARNES_HUT, theta=0.5)

        E = kinetic_energy(vel, masses) + potential_energy(pos, masses)
        rel_error = abs(E - E0) / abs(E0)
        assert rel_error < 0.05, \
            f"Forest-Ruth BH energy drift: {rel_error:.2e} after {n_steps} steps"

    def test_eccentric_orbit_verlet(self, kepler_eccentric):
        """Eccentric orbit (e=0.5) with Verlet should conserve energy."""

        pos, vel, masses, ids = kepler_eccentric
        dt = 0.001
        n_steps = 6283

        _, _, energies = run_simulation(
            step_velocity_verlet, pos, vel, masses, ids, dt, n_steps,
            needs_accelerations=True
        )

        E0 = energies[0]
        max_rel_error = np.max(np.abs((energies - E0) / abs(E0)))

        assert max_rel_error < 1e-4, \
            f"Eccentric orbit energy error: {max_rel_error:.2e}"


class TestMonolithicSteps:
    """Energy conservation of the steppers the monolithic workers call."""

    def test_step_bodies_direct_energy_conservation(self, kepler_circular):
        """step_direct with symplectic Euler should conserve energy."""

        pos, vel, masses, ids = kepler_circular
        dt = 0.001
        n_steps = 6283

        E0 = kinetic_energy(vel, masses) + potential_energy(pos, masses)

        for _ in range(n_steps):
            pos, vel, _ = step_direct(pos, vel, masses, ids, dt, use_symplectic=True)

        E = kinetic_energy(vel, masses) + potential_energy(pos, masses)
        rel_error = abs(E - E0) / abs(E0)
        assert rel_error < 0.01, \
            f"step_direct energy error: {rel_error:.2e}"

    def test_step_bodies_velocity_verlet_energy_conservation(self, kepler_circular):
        """step_velocity_verlet should conserve energy over 1 period."""

        pos, vel, masses, ids = kepler_circular
        dt = 0.001
        n_steps = 6283

        E0 = kinetic_energy(vel, masses) + potential_energy(pos, masses)
        accel = compute_accelerations(pos, masses, ids)

        for _ in range(n_steps):
            pos, vel, accel = step_velocity_verlet(
                pos, vel, masses, ids, accel, dt
            )

        E = kinetic_energy(vel, masses) + potential_energy(pos, masses)
        rel_error = abs(E - E0) / abs(E0)
        assert rel_error < 1e-4, \
            f"step_velocity_verlet energy error: {rel_error:.2e}"
