# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Tests for adaptive timestepping: dt estimation and simulation behavior."""
import numpy as np

from constants import SOFTENING_SQ
from diagnostics import kinetic_energy, potential_energy
from physics import compute_accelerations, estimate_adaptive_dt, estimate_adaptive_dt_from_amax, step_velocity_verlet


class TestEstimateAdaptiveDt:
    """Tests for the estimate_adaptive_dt function."""

    def test_large_acceleration_small_dt(self):
        """Large acceleration should produce smaller dt than small acceleration."""

        accel_large = np.array([[0.0, 0.0, 1000.0]], dtype=np.float64)
        accel_small = np.array([[0.0, 0.0, 0.1]], dtype=np.float64)

        dt_large = estimate_adaptive_dt(accel_large, 0.001, eta=0.02, dt_max=1.0)
        dt_small = estimate_adaptive_dt(accel_small, 0.001, eta=0.02, dt_max=1.0)

        assert dt_large < dt_small, \
            f"Large accel dt ({dt_large:.2e}) should be < small accel dt ({dt_small:.2e})"
        assert dt_large < 0.001, "dt should be very small for a_max=1000"

    def test_zero_acceleration_returns_dt_max(self):
        """Zero accelerations should return dt_max."""

        accel_zero = np.zeros((10, 3), dtype=np.float64)
        dt = estimate_adaptive_dt(accel_zero, 0.001, dt_max=0.05)
        assert dt == 0.05, f"Expected dt_max=0.05, got {dt}"

    def test_clamped_to_bounds(self):
        """The result is clamped to [dt_min, dt_max]."""

        # Very large acceleration would produce tiny dt → clamped to dt_min
        accel_huge = np.array([[0.0, 0.0, 1e20]], dtype=np.float64)
        dt = estimate_adaptive_dt(accel_huge, 0.001, dt_min=1e-6, dt_max=0.1)
        assert dt == 1e-6, f"dt ({dt:.2e}) should be dt_min (1e-6)"

        # Very small acceleration → clamped to dt_max
        accel_tiny = np.array([[0.0, 0.0, 1e-30]], dtype=np.float64)
        dt = estimate_adaptive_dt(accel_tiny, 0.001, dt_min=1e-10, dt_max=0.05)
        assert dt == 0.05, f"dt ({dt:.2e}) should be dt_max (0.05)"

    def test_eta_scaling(self):
        """Doubling eta should roughly double dt."""

        accel = np.array([[0.0, 0.0, 10.0]], dtype=np.float64)
        dt1 = estimate_adaptive_dt(accel, 0.001, eta=0.01, dt_max=1.0)
        dt2 = estimate_adaptive_dt(accel, 0.001, eta=0.02, dt_max=1.0)

        ratio = dt2 / dt1
        assert 1.8 < ratio < 2.2, \
            f"Doubling eta should ~double dt, got ratio={ratio:.2f}"

    def test_from_amax_matches_array_version(self):
        """estimate_adaptive_dt_from_amax should match estimate_adaptive_dt."""

        accel = np.array([[3.0, 4.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
        # a_max = 5.0 (body 0: sqrt(9+16) = 5)
        dt_array = estimate_adaptive_dt(accel, 0.001, eta=0.02, dt_max=1.0)
        dt_scalar = estimate_adaptive_dt_from_amax(5.0, 0.001, eta=0.02, dt_max=1.0)

        assert abs(dt_array - dt_scalar) < 1e-15, \
            f"Array version ({dt_array:.6e}) should match scalar version ({dt_scalar:.6e})"


class TestAdaptiveSimulation:
    """Integration tests: adaptive dt applied to actual N-body simulations."""

    def test_circular_orbit_dt_stays_constant(self, kepler_circular):
        """Circular orbit has constant a_max → dt should barely change."""

        pos, vel, masses, ids = kepler_circular
        dt = 0.001
        softening = SOFTENING_SQ ** 0.5
        accel = compute_accelerations(pos, masses, ids)

        dt_values = []
        for _ in range(200):
            dt = estimate_adaptive_dt(accel, dt, eta=0.02, softening=softening, dt_max=0.01)
            dt_values.append(dt)
            pos, vel, accel = step_velocity_verlet(pos, vel, masses, ids, accel, dt)

        dt_arr = np.array(dt_values)
        cv = np.std(dt_arr) / np.mean(dt_arr)  # coefficient of variation
        assert cv < 0.05, \
            f"Circular orbit dt should be ~constant, CV={cv:.4f} (mean={np.mean(dt_arr):.4e})"

    def test_eccentric_orbit_dt_varies(self, kepler_highly_eccentric):
        """Highly eccentric orbit (e=0.9): dt should shrink at periapsis, grow at apoapsis."""

        pos, vel, masses, ids = kepler_highly_eccentric
        dt = 0.001
        softening = SOFTENING_SQ ** 0.5
        accel = compute_accelerations(pos, masses, ids)

        dt_values = []
        for _ in range(5000):
            dt = estimate_adaptive_dt(accel, dt, eta=0.02, softening=softening,
                                       dt_min=1e-8, dt_max=0.01)
            dt_values.append(dt)
            pos, vel, accel = step_velocity_verlet(pos, vel, masses, ids, accel, dt)

        dt_arr = np.array(dt_values)
        dt_min_seen = np.min(dt_arr)
        dt_max_seen = np.max(dt_arr)
        ratio = dt_max_seen / dt_min_seen

        assert ratio > 1.5, \
            f"Highly eccentric orbit should have dt variation > 1.5x, got ratio={ratio:.2f}"

    def test_eccentric_orbit_energy_conserved(self, kepler_eccentric):
        """Adaptive VV on eccentric orbit should conserve energy."""

        pos, vel, masses, ids = kepler_eccentric
        dt = 0.001
        softening = SOFTENING_SQ ** 0.5
        accel = compute_accelerations(pos, masses, ids)

        E0 = kinetic_energy(vel, masses) + potential_energy(pos, masses)

        for _ in range(3000):
            dt = estimate_adaptive_dt(accel, dt, eta=0.02, softening=softening,
                                       dt_min=1e-8, dt_max=0.01)
            pos, vel, accel = step_velocity_verlet(pos, vel, masses, ids, accel, dt)

        E = kinetic_energy(vel, masses) + potential_energy(pos, masses)
        rel_error = abs(E - E0) / abs(E0)

        assert rel_error < 0.01, \
            f"Adaptive VV energy error: {rel_error:.2e} (should be < 1%)"


def test_a_nan_acceleration_gives_the_smallest_step():
    """NaN compared false everywhere: a diverged state got dt_max."""
    acc = np.array([[1.0, 0.0, 0.0], [np.nan, 0.0, 0.0]])
    assert estimate_adaptive_dt(acc, 0.01, eta=0.02, softening=0.1, dt_min=1e-6, dt_max=0.1) == 1e-6
    assert estimate_adaptive_dt_from_amax(float("nan"), 0.01, eta=0.02, softening=0.1,
                                          dt_min=1e-6, dt_max=0.1) == 1e-6
