# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Tests comparing Barnes-Hut tree code against direct O(N^2) computation."""
import numpy as np
import pytest

from barnes_hut import build_octree, compute_accelerations_barnes_hut
from diagnostics import kinetic_energy, potential_energy
from helpers import random_system
from physics import BARNES_HUT, compute_accelerations, step_direct, step_rk4, step_velocity_verlet


class TestBarnesHutAccuracy:
    """Barnes-Hut forces should approximate direct forces within theta-dependent bound."""

    def test_barnes_hut_matches_direct(self, plummer_sphere_small):
        """Barnes-Hut (theta=0.5) should match direct forces within ~5% per body."""

        pos, vel, masses, ids = plummer_sphere_small

        accel_direct = compute_accelerations(pos, masses, ids)
        accel_bh = compute_accelerations_barnes_hut(pos, masses, ids, theta=0.5)

        for i in range(len(masses)):
            direct_mag = np.linalg.norm(accel_direct[i])
            if direct_mag < 1e-15:
                continue
            error = np.linalg.norm(accel_bh[i] - accel_direct[i]) / direct_mag
            assert error < 0.05, \
                f"Body {i}: Barnes-Hut error {error:.4f} exceeds 5% with theta=0.5"

    def test_barnes_hut_theta_zero_exact(self, plummer_sphere_small):
        """Barnes-Hut with theta=0 should be equivalent to direct summation."""

        pos, vel, masses, ids = plummer_sphere_small

        accel_direct = compute_accelerations(pos, masses, ids)
        accel_bh = compute_accelerations_barnes_hut(pos, masses, ids, theta=0.0)

        np.testing.assert_allclose(
            accel_bh, accel_direct, rtol=1e-6, atol=1e-14,
            err_msg="Barnes-Hut with theta=0 should match direct exactly"
        )

    def test_smaller_theta_more_accurate(self, plummer_sphere_small):
        """Smaller theta should produce results closer to direct."""

        pos, vel, masses, ids = plummer_sphere_small
        accel_direct = compute_accelerations(pos, masses, ids)

        errors = {}
        for theta in [0.3, 0.5, 0.8]:
            accel_bh = compute_accelerations_barnes_hut(pos, masses, ids, theta=theta)
            rms_err = 0.0
            count = 0
            for i in range(len(masses)):
                direct_mag = np.linalg.norm(accel_direct[i])
                if direct_mag < 1e-15:
                    continue
                rms_err += (np.linalg.norm(accel_bh[i] - accel_direct[i]) / direct_mag) ** 2
                count += 1
            errors[theta] = np.sqrt(rms_err / max(count, 1))

        assert errors[0.3] <= errors[0.8] * 1.1, \
            f"Smaller theta should give less error: {errors}"

    def test_barnes_hut_rk4_energy_bounded(self, plummer_sphere_small):
        """Barnes-Hut RK4 should conserve energy reasonably over short run."""

        pos, vel, masses, ids = plummer_sphere_small
        dt = 0.001
        n_steps = 50

        E0 = (kinetic_energy(vel, masses) +
              potential_energy(pos, masses))

        for _ in range(n_steps):
            pos, vel, _ = step_rk4(pos, vel, masses, ids, dt, algorithm=BARNES_HUT, theta=0.5)

        E = (kinetic_energy(vel, masses) +
             potential_energy(pos, masses))

        rel_error = abs(E - E0) / abs(E0)
        assert rel_error < 0.05, \
            f"Barnes-Hut RK4 energy drift: {rel_error:.2e} after {n_steps} steps"

    def test_barnes_hut_vs_direct_trajectory(self, plummer_sphere_small):
        """Barnes-Hut and direct RK4 should produce similar trajectories."""

        pos, vel, masses, ids = plummer_sphere_small
        dt = 0.001
        n_steps = 10

        # Direct
        pos_d, vel_d = pos.copy(), vel.copy()
        for _ in range(n_steps):
            pos_d, vel_d, _ = step_rk4(pos_d, vel_d, masses, ids, dt)

        # Barnes-Hut
        pos_bh, vel_bh = pos.copy(), vel.copy()
        for _ in range(n_steps):
            pos_bh, vel_bh, _ = step_rk4(pos_bh, vel_bh, masses, ids, dt, algorithm=BARNES_HUT, theta=0.5)

        diff = pos_d - pos_bh
        rmse = np.sqrt(np.mean(np.sum(diff**2, axis=1)))
        assert rmse < 0.01, \
            f"Barnes-Hut vs Direct position RMSE = {rmse:.4e} after {n_steps} steps"


class TestBarnesHutMonolithicWrappers:
    """The monolithic steppers with the Barnes-Hut force algorithm."""

    def test_barnes_hut_euler_matches_direct(self, plummer_sphere_small):
        """the Barnes-Hut Euler step should produce results close to direct Euler."""

        pos, vel, masses, ids = plummer_sphere_small
        dt = 0.001
        n_steps = 5

        pos_d, vel_d = pos.copy(), vel.copy()
        pos_bh, vel_bh = pos.copy(), vel.copy()

        for _ in range(n_steps):
            pos_d, vel_d, _ = step_direct(pos_d, vel_d, masses, ids, dt, use_symplectic=True)
            pos_bh, vel_bh, _ = step_direct(
                pos_bh, vel_bh, masses, ids, dt, algorithm=BARNES_HUT, theta=0.5, use_symplectic=True
            )

        diff = pos_d - pos_bh
        rmse = np.sqrt(np.mean(np.sum(diff**2, axis=1)))
        assert rmse < 0.01, \
            f"Barnes-Hut Euler vs direct RMSE = {rmse:.4e}"

    def test_barnes_hut_euler_energy_bounded(self, plummer_sphere_small):
        """the Barnes-Hut Euler step should keep energy approximately bounded."""

        pos, vel, masses, ids = plummer_sphere_small
        dt = 0.001
        n_steps = 100

        E0 = kinetic_energy(vel, masses) + potential_energy(pos, masses)

        for _ in range(n_steps):
            pos, vel, _ = step_direct(
                pos, vel, masses, ids, dt, algorithm=BARNES_HUT, theta=0.5, use_symplectic=True
            )

        E = kinetic_energy(vel, masses) + potential_energy(pos, masses)
        rel_error = abs(E - E0) / abs(E0)
        assert rel_error < 0.05, \
            f"Barnes-Hut Euler energy drift: {rel_error:.2e}"

    def test_barnes_hut_simple_euler(self, plummer_sphere_small):
        """the Barnes-Hut simple Euler step should work."""

        pos, vel, masses, ids = plummer_sphere_small
        dt = 0.001

        E0 = kinetic_energy(vel, masses) + potential_energy(pos, masses)

        for _ in range(50):
            pos, vel, _ = step_direct(
                pos, vel, masses, ids, dt, algorithm=BARNES_HUT, theta=0.5, use_symplectic=False
            )

        E = kinetic_energy(vel, masses) + potential_energy(pos, masses)
        rel_error = abs(E - E0) / abs(E0)
        assert rel_error < 0.1, \
            f"Barnes-Hut simple Euler energy drift: {rel_error:.2e}"

    def test_barnes_hut_velocity_verlet_energy(self, plummer_sphere_small):
        """Barnes-Hut velocity Verlet should conserve energy."""

        pos, vel, masses, ids = plummer_sphere_small
        dt = 0.001
        n_steps = 100

        E0 = kinetic_energy(vel, masses) + potential_energy(pos, masses)
        accel = compute_accelerations_barnes_hut(pos, masses, ids, theta=0.5)

        for _ in range(n_steps):
            pos, vel, accel = step_velocity_verlet(
                pos, vel, masses, ids, accel, dt, algorithm=BARNES_HUT, theta=0.5
            )

        E = kinetic_energy(vel, masses) + potential_energy(pos, masses)
        rel_error = abs(E - E0) / abs(E0)
        assert rel_error < 0.02, \
            f"Barnes-Hut velocity Verlet energy drift: {rel_error:.2e}"

    def test_step_bodies_bh_vv_matches_direct_vv(self, plummer_sphere_small):
        """BH Velocity Verlet trajectory should be close to direct VV."""

        pos, vel, masses, ids = plummer_sphere_small
        dt = 0.001
        n_steps = 10

        # Direct VV
        pos_d, vel_d = pos.copy(), vel.copy()
        acc_d = compute_accelerations(pos_d, masses, ids)
        for _ in range(n_steps):
            pos_d, vel_d, acc_d = step_velocity_verlet(pos_d, vel_d, masses, ids, acc_d, dt)

        # BH VV
        pos_bh, vel_bh = pos.copy(), vel.copy()
        acc_bh = compute_accelerations_barnes_hut(pos_bh, masses, ids, theta=0.5)
        for _ in range(n_steps):
            pos_bh, vel_bh, acc_bh = step_velocity_verlet(
                pos_bh, vel_bh, masses, ids, acc_bh, dt, algorithm=BARNES_HUT, theta=0.5
            )

        diff = pos_d - pos_bh
        rmse = np.sqrt(np.mean(np.sum(diff**2, axis=1)))
        assert rmse < 0.01, \
            f"BH VV vs Direct VV position RMSE = {rmse:.4e}"


class TestBarnesHutClusteredBodies:
    """Coincident and near-coincident bodies are aggregated into the deepest
    leaf, not dropped from the tree when insertion hits the maximum depth (nor
    allowed to overrun the node array)."""

    @staticmethod
    def _clustered(seed=4):
        rng = np.random.default_rng(seed)
        pos = rng.normal(size=(40, 3))
        pos[10] = pos[11] = pos[12] = pos[3]          # three exact duplicates of body 3
        pos[20] = pos[21] + 1e-17                      # below the depth-50 cell size
        masses = rng.uniform(0.5, 1.5, 40) / 40
        ids = np.arange(1, 41, dtype=np.int64)
        return pos, masses, ids

    def test_every_body_is_in_the_tree(self):
        pos, masses, ids = self._clustered()
        tree, n = build_octree(pos, masses)
        leaves = tree[tree['is_leaf'] == 1]
        assert int(leaves['count'].sum()) == len(masses)
        assert np.isclose(tree[0]['mass'], masses.sum(), rtol=1e-14)
        assert (leaves['body_index'] == -2).any()      # the duplicates were aggregated

    def test_matches_direct_summation_with_tiny_theta(self):
        pos, masses, ids = self._clustered()
        eps2 = 1e-4
        a_direct = compute_accelerations(pos, masses, ids, softening_sq=eps2)
        a_bh = compute_accelerations_barnes_hut(pos, masses, ids, theta=1e-9, softening_sq=eps2)
        assert np.all(np.isfinite(a_bh))
        np.testing.assert_allclose(a_bh, a_direct, rtol=1e-9, atol=1e-12)

    def test_two_nearly_coincident_bodies_do_not_exhaust_the_node_array(self):
        pos = np.array([[0.1, 0.1, 0.1], [0.1 + 1e-12, 0.1, 0.1], [1.0, 1.0, 1.0]])
        tree, n = build_octree(pos, np.array([1.0, 2.0, 4.0]))   # pair separable only deep down
        assert n > 5 * 3 + 16                        # needed more than the initial capacity
        assert np.isclose(tree[0]['mass'], 7.0)

    def test_two_coincident_bodies_aggregate_at_once(self):
        tree, n = build_octree(np.zeros((2, 3)), np.array([1.0, 2.0]))
        assert n == 1                                # no subdivision at all
        assert tree[0]['body_index'] == -2 and tree[0]['count'] == 2
        assert np.isclose(tree[0]['mass'], 3.0)


# --------------------------------------------------------------------------- #
# Coincident bodies aggregate instead of subdividing to max depth
# --------------------------------------------------------------------------- #

def test_coincident_pairs_do_not_blow_up_tree():
    """Without aggregation each exactly coincident pair would subdivide all
    MAX_TREE_DEPTH levels (~180 nodes per body)."""
    rng = np.random.default_rng(1)
    n = 4000                                        # 2000 coincident pairs
    pos = np.repeat(rng.normal(size=(n // 2, 3)), 2, axis=0)
    _, num_nodes = build_octree(pos, np.ones(n))
    assert num_nodes < 10 * n


def test_coincident_triples_aggregate_into_one_leaf():
    pos = np.repeat(np.random.default_rng(4).normal(size=(50, 3)), 3, axis=0)
    masses = np.random.default_rng(5).random(150) + 0.1
    tree, num_nodes = build_octree(pos, masses)
    leaves = tree[(tree["is_leaf"] == 1) & (tree["count"] > 0)]
    assert len(leaves) == 50 and np.all(leaves["count"] == 3)
    assert np.isclose(leaves["mass"].sum(), masses.sum())
    assert num_nodes < 20 * 150


@pytest.mark.parametrize("eps2", [1e-4, 1.0])
def test_coincident_bodies_match_direct_summation(eps2):
    rng = np.random.default_rng(1)
    n = 64
    pos = rng.normal(size=(n, 3))
    m = rng.random(n) + 0.1
    ids = np.arange(n, dtype=np.int64)
    pos[1] = pos[0]
    pos[7] = pos[6]
    pos[8] = pos[6]
    pos[5] = pos[4] + 1e-13
    a_bh = compute_accelerations_barnes_hut(pos, m, ids, 0.1, 1.0, eps2)
    a_d = compute_accelerations(pos, m, ids, softening_sq=eps2)
    rel = np.linalg.norm(a_bh - a_d, axis=1) / np.linalg.norm(a_d, axis=1)
    assert rel[[0, 1, 4, 5, 6, 7, 8]].max() < 1e-6   # coincident / near-coincident
    assert rel.max() < 1e-3                          # theta = 0.1 elsewhere


def test_coincident_bodies_unsoftened_stay_finite():
    """With eps^2 = 0 the aggregated leaf at the body's own position is skipped
    rather than divided by zero."""
    pos, _, m, ids = random_system(n=32)
    pos[1] = pos[0]
    m = m.copy()
    m[1] = 3 * m[0]                                 # unequal: COM not exactly a midpoint
    a = compute_accelerations_barnes_hut(pos, m, ids, 0.5, 1.0, 0.0)
    assert np.isfinite(a).all()


@pytest.mark.parametrize("theta", [1.0, 1.5, 2.0])
def test_a_cell_containing_the_target_is_always_opened(theta):
    """Bodies 0 and 1 share an octant, at opposite corners. Seen from body 0
    that octant (COM halfway along its diagonal) passes the opening criterion
    for theta > 1/sqrt(3), so accepting it would put body 0's own mass into its
    force. Opened, it leaves only leaves, so bodies 0 and 1 get the direct
    force (body 2 legitimately sees their octant as one monopole)."""
    pos = np.array([[0.01, 0.01, 0.01], [0.99, 0.99, 0.99], [-1.0, -1.0, -1.0]])
    masses = np.ones(3)
    ids = np.arange(3, dtype=np.int32)
    direct = compute_accelerations(pos, masses, ids, 1e-4)
    bh = compute_accelerations_barnes_hut(pos, masses, ids, theta, 1.0, 1e-4)
    np.testing.assert_allclose(bh[:2], direct[:2], rtol=1e-12, atol=0)
