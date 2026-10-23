# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Tests for gravitational force computation correctness."""
import numpy as np

from constants import G_CONST
from physics import compute_accelerations, compute_package_accelerations


class TestComputeAccelerations:
    """Test compute_accelerations from physics.py."""

    def test_two_body_force_direction(self):
        """Body 0 at origin, body 1 at (1,0,0). Force on 0 should point toward 1 (+x)."""

        positions = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float64)
        masses = np.array([1.0, 1.0], dtype=np.float64)
        ids = np.array([0, 1], dtype=np.int32)

        accel = compute_accelerations(positions, masses, ids)

        assert accel[0, 0] > 0, "Body 0 should accelerate in +x toward body 1"
        assert abs(accel[0, 1]) < 1e-15, "No y-component for x-aligned pair"
        assert abs(accel[0, 2]) < 1e-15, "No z-component for x-aligned pair"
        assert accel[1, 0] < 0, "Body 1 should accelerate in -x toward body 0"

    def test_two_body_force_magnitude(self):
        """Force magnitude should be G*M/r^2 (with softening negligible at r=1)."""

        r = 1.0
        m = 1.0
        positions = np.array([[0.0, 0.0, 0.0], [r, 0.0, 0.0]], dtype=np.float64)
        masses = np.array([m, m], dtype=np.float64)
        ids = np.array([0, 1], dtype=np.int32)

        accel = compute_accelerations(positions, masses, ids)

        expected = G_CONST * m / (r * r)
        assert abs(accel[0, 0] - expected) / expected < 1e-6, \
            f"Force magnitude mismatch: got {accel[0, 0]}, expected {expected}"

    def test_newton_third_law(self):
        """m0*a0 + m1*a1 = 0 (Newton's third law)."""

        positions = np.array([[0.0, 0.0, 0.0], [1.5, 0.7, -0.3]], dtype=np.float64)
        masses = np.array([2.0, 3.0], dtype=np.float64)
        ids = np.array([0, 1], dtype=np.int32)

        accel = compute_accelerations(positions, masses, ids)

        total_force = masses[0] * accel[0] + masses[1] * accel[1]
        assert np.linalg.norm(total_force) < 1e-10, \
            f"Newton's 3rd law violated: net force = {total_force}"

    def test_self_interaction_excluded(self):
        """Bodies with same ID should not interact."""

        positions = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float64)
        masses = np.array([1.0, 1.0], dtype=np.float64)
        ids = np.array([5, 5], dtype=np.int32)

        accel = compute_accelerations(positions, masses, ids)
        assert np.allclose(accel, 0.0), "Bodies with same ID should not interact"

    def test_three_body_superposition(self):
        """Force on body 0 from bodies 1 and 2 should be vector sum."""

        positions = np.array([
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ], dtype=np.float64)
        masses = np.array([1.0, 1.0, 1.0], dtype=np.float64)
        ids = np.array([0, 1, 2], dtype=np.int32)

        accel = compute_accelerations(positions, masses, ids)

        assert accel[0, 0] > 0, "Body 0 should be pulled in +x by body 1"
        assert accel[0, 1] > 0, "Body 0 should be pulled in +y by body 2"
        assert abs(accel[0, 0] - accel[0, 1]) < 1e-10, "Symmetric forces should be equal"

    def test_softening_prevents_singularity(self):
        """Two bodies at same position should not produce infinite force."""

        positions = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]], dtype=np.float64)
        masses = np.array([1.0, 1.0], dtype=np.float64)
        ids = np.array([0, 1], dtype=np.int32)

        accel = compute_accelerations(positions, masses, ids)
        assert np.all(np.isfinite(accel)), f"Acceleration should be finite, got {accel}"

    def test_inverse_square_law_scaling(self):
        """Doubling distance should reduce force by factor 4."""

        m = 1.0
        ids = np.array([0, 1], dtype=np.int32)
        masses = np.array([m, m], dtype=np.float64)

        pos_r2 = np.array([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0]], dtype=np.float64)
        accel_r2 = compute_accelerations(pos_r2, masses, ids)

        pos_r4 = np.array([[0.0, 0.0, 0.0], [4.0, 0.0, 0.0]], dtype=np.float64)
        accel_r4 = compute_accelerations(pos_r4, masses, ids)

        ratio = accel_r2[0, 0] / accel_r4[0, 0]
        assert abs(ratio - 4.0) < 0.01, f"Inverse-square ratio should be 4, got {ratio}"

    def test_multi_body_newton_third_law(self, plummer_sphere_small):
        """Sum of all forces should be zero for N-body system."""

        pos, vel, masses, ids = plummer_sphere_small
        accel = compute_accelerations(pos, masses, ids)

        # Total force = sum(m_i * a_i) should be zero
        total_force = (masses[:, None] * accel).sum(axis=0)
        assert np.linalg.norm(total_force) < 1e-8, \
            f"Total force should be zero, got magnitude {np.linalg.norm(total_force):.2e}"


class TestPackageAccelerations:
    """Test compute_package_accelerations (distributed worker force computation)."""

    def test_package_matches_full(self):
        """Package acceleration for a subset should match full computation."""

        np.random.seed(123)
        n = 20
        positions = np.random.randn(n, 3).astype(np.float64)
        masses = np.abs(np.random.randn(n).astype(np.float64)) + 0.1
        ids = np.arange(n, dtype=np.int32)

        full_accel = compute_accelerations(positions, masses, ids)

        k = 5
        k_pos = positions[:k].copy()
        k_ids = ids[:k].copy()
        pkg_accel = compute_package_accelerations(k_pos, k_ids, positions, masses, ids)

        np.testing.assert_allclose(
            pkg_accel, full_accel[:k],
            rtol=1e-10, atol=1e-14,
            err_msg="Package accelerations should match full computation"
        )

    def test_package_different_subsets(self):
        """Different package subsets should all match full computation."""

        np.random.seed(456)
        n = 15
        positions = np.random.randn(n, 3).astype(np.float64)
        masses = np.abs(np.random.randn(n).astype(np.float64)) + 0.1
        ids = np.arange(n, dtype=np.int32)

        full_accel = compute_accelerations(positions, masses, ids)

        # Test middle subset
        k_start, k_end = 5, 10
        k_pos = positions[k_start:k_end].copy()
        k_ids = ids[k_start:k_end].copy()
        pkg_accel = compute_package_accelerations(k_pos, k_ids, positions, masses, ids)

        np.testing.assert_allclose(
            pkg_accel, full_accel[k_start:k_end],
            rtol=1e-10, atol=1e-14,
            err_msg="Middle package should match full computation"
        )
