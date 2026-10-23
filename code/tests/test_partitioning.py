# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Tests for V2 package partitioning (permute-once strategies).

The load-bearing guarantee is that a partition is *only* a work-assignment: it
must be a disjoint, covering permutation of the bodies, and reordering the bodies
cannot change any physical quantity of the system (total energy, per-body data).
These tests pin both properties plus the reproducibility and locality contracts.
"""

import os

import numpy as np
import pytest

from v2.partitioning import (
    DEFAULT_PARTITION_SEED,
    LINEAR,
    ORB,
    RANDOM,
    SPATIAL,
    VALID_PARTITION_STRATEGIES,
    apply_partition,
    compute_permutation,
    morton_keys,
    orb_order,
)


def _make_bodies(n, seed=1):
    """A small random cloud of body dicts in the format bodies_to_binary expects."""
    rng = np.random.default_rng(seed)
    pos = rng.uniform(-10.0, 10.0, size=(n, 3))
    vel = rng.uniform(-1.0, 1.0, size=(n, 3))
    mass = rng.uniform(0.1, 2.0, size=n)
    return [
        {
            "n": i,
            "idBody": 1000 + i,
            "m": float(mass[i]),
            "x": float(pos[i, 0]),
            "y": float(pos[i, 1]),
            "z": float(pos[i, 2]),
            "vx": float(vel[i, 0]),
            "vy": float(vel[i, 1]),
            "vz": float(vel[i, 2]),
        }
        for i in range(n)
    ]


def _total_energy(bodies):
    """Total kinetic + gravitational potential energy (G=1), order-independent."""
    m = np.array([b["m"] for b in bodies])
    p = np.array([(b["x"], b["y"], b["z"]) for b in bodies])
    v = np.array([(b["vx"], b["vy"], b["vz"]) for b in bodies])
    ke = 0.5 * np.sum(m * np.sum(v * v, axis=1))
    pe = 0.0
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            r = np.linalg.norm(p[i] - p[j])
            pe -= m[i] * m[j] / r
    return ke + pe


# ---------------------------------------------------------------------------
# Permutation invariant: disjoint + covering for every strategy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("strategy", VALID_PARTITION_STRATEGIES)
@pytest.mark.parametrize("n", [1, 2, 7, 100, 1001])
def test_permutation_is_exact_bijection(strategy, n):
    bodies = _make_bodies(n)
    perm = compute_permutation(strategy, bodies, seed=DEFAULT_PARTITION_SEED)

    assert perm.shape == (n,)
    assert np.issubdtype(perm.dtype, np.integer)
    # Exactly the set {0, ..., n-1}, each once -> disjoint + covering.
    assert np.array_equal(np.sort(perm), np.arange(n))


def test_linear_is_identity():
    bodies = _make_bodies(50)
    perm = compute_permutation(LINEAR, bodies)
    assert np.array_equal(perm, np.arange(50))
    # apply_partition returns the list untouched (same object) for linear.
    assert apply_partition(bodies, LINEAR) is bodies


def test_unknown_strategy_raises():
    with pytest.raises(ValueError):
        compute_permutation("hilbert", _make_bodies(4))


# ---------------------------------------------------------------------------
# Physics invariance: reordering bodies cannot change the system
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("strategy", VALID_PARTITION_STRATEGIES)
def test_energy_invariant_under_partition(strategy):
    bodies = _make_bodies(60)
    e_before = _total_energy(bodies)
    reordered = apply_partition(bodies, strategy, seed=7)
    e_after = _total_energy(reordered)

    # Same body count, same idBody multiset -> disjoint + covering, nothing lost.
    assert len(reordered) == len(bodies)
    assert {b["idBody"] for b in reordered} == {b["idBody"] for b in bodies}
    # Total energy is a global sum, so any true permutation leaves it unchanged.
    assert np.isclose(e_after, e_before, rtol=0, atol=1e-9)


@pytest.mark.parametrize("strategy", VALID_PARTITION_STRATEGIES)
def test_apply_partition_does_not_mutate_input(strategy):
    bodies = _make_bodies(30)
    snapshot = [dict(b) for b in bodies]
    apply_partition(bodies, strategy, seed=3)
    assert bodies == snapshot  # underlying dicts untouched


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------

def test_random_is_seed_reproducible():
    bodies = _make_bodies(200)
    a = compute_permutation(RANDOM, bodies, seed=42)
    b = compute_permutation(RANDOM, bodies, seed=42)
    assert np.array_equal(a, b)


def test_random_differs_by_seed():
    bodies = _make_bodies(200)
    a = compute_permutation(RANDOM, bodies, seed=1)
    b = compute_permutation(RANDOM, bodies, seed=2)
    assert not np.array_equal(a, b)  # astronomically unlikely to collide for N=200


def test_random_actually_shuffles():
    bodies = _make_bodies(500)
    perm = compute_permutation(RANDOM, bodies, seed=5)
    assert not np.array_equal(perm, np.arange(500))


def test_spatial_is_deterministic_from_positions():
    bodies = _make_bodies(300)
    a = compute_permutation(SPATIAL, bodies)
    b = compute_permutation(SPATIAL, bodies)
    assert np.array_equal(a, b)


# ---------------------------------------------------------------------------
# Morton / spatial locality
# ---------------------------------------------------------------------------

def test_morton_keys_equal_for_coincident_points():
    bodies = [
        {"x": 1.0, "y": 2.0, "z": 3.0},
        {"x": 1.0, "y": 2.0, "z": 3.0},
    ]
    pos = np.array([(b["x"], b["y"], b["z"]) for b in bodies])
    keys = morton_keys(pos)
    assert keys[0] == keys[1]


def test_spatial_orders_by_locality():
    """Consecutive bodies in Morton order should be closer than random pairs.

    Build a uniform 3D grid, then compare the mean distance between
    order-adjacent points under the spatial permutation to the mean distance of
    the same number of random pairs. Z-order should be markedly tighter.
    """
    g = 10
    coords = [(x, y, z) for x in range(g) for y in range(g) for z in range(g)]
    rng = np.random.default_rng(0)
    order = rng.permutation(len(coords))  # start scrambled so linear order can't cheat
    bodies = [
        {
            "n": i,
            "idBody": i,
            "m": 1.0,
            "x": float(coords[order[i]][0]),
            "y": float(coords[order[i]][1]),
            "z": float(coords[order[i]][2]),
            "vx": 0.0,
            "vy": 0.0,
            "vz": 0.0,
        }
        for i in range(len(coords))
    ]
    reordered = apply_partition(bodies, SPATIAL)
    pts = np.array([(b["x"], b["y"], b["z"]) for b in reordered])

    adjacent = np.linalg.norm(np.diff(pts, axis=0), axis=1).mean()

    all_pts = np.array([(b["x"], b["y"], b["z"]) for b in bodies])
    ii = rng.integers(0, len(all_pts), size=2000)
    jj = rng.integers(0, len(all_pts), size=2000)
    random_pair = np.linalg.norm(all_pts[ii] - all_pts[jj], axis=1).mean()

    assert adjacent < 0.5 * random_pair


# ---------------------------------------------------------------------------
# ORB: orthogonal recursive bisection with cuts on the package boundaries
# ---------------------------------------------------------------------------

def _positions(bodies):
    return np.array([(b["x"], b["y"], b["z"]) for b in bodies])


def _blocks(perm, n, package_size):
    return [perm[i:i + package_size] for i in range(0, n, package_size)]


@pytest.mark.parametrize("n,package_size", [(1001, 126), (1000, 125), (100, 7), (64, 64), (5, 100)])
def test_orb_packages_are_separated_by_axis_aligned_cuts(n, package_size):
    """Any two ORB packages are separated along some axis: one's maximum is at
    most the other's minimum (the bisection planes)."""
    bodies = _make_bodies(n, seed=5)
    pos = _positions(bodies)
    perm = compute_permutation(ORB, bodies, package_size=package_size)
    assert sorted(perm.tolist()) == list(range(n))
    blocks = _blocks(perm, n, package_size)
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            a, b = pos[blocks[i]], pos[blocks[j]]
            assert any(a[:, k].max() <= b[:, k].min() or b[:, k].max() <= a[:, k].min()
                       for k in range(3)), (i, j)


def test_orb_single_package_is_identity():
    bodies = _make_bodies(40)
    assert np.array_equal(compute_permutation(ORB, bodies, package_size=40), np.arange(40))


def test_orb_without_package_size_is_a_kd_ordering():
    bodies = _make_bodies(33)
    perm = compute_permutation(ORB, bodies)
    assert sorted(perm.tolist()) == list(range(33))


def test_orb_handles_degenerate_positions():
    for pos in (np.zeros((10, 3)), np.c_[np.arange(10.0), np.zeros(10), np.zeros(10)],
                np.full((7, 3), np.nan)):
        perm = orb_order(pos, package_size=3)
        assert sorted(perm.tolist()) == list(range(len(pos)))
    assert orb_order(np.empty((0, 3)), package_size=3).size == 0


def test_orb_is_deterministic_and_ignores_the_seed():
    bodies = _make_bodies(200)
    a = compute_permutation(ORB, bodies, seed=1, package_size=25)
    b = compute_permutation(ORB, bodies, seed=99, package_size=25)
    assert np.array_equal(a, b)


def test_orb_cut_fraction_on_the_disc_model():
    """The force-weighted cut fraction of ORB on code/data/gali.txt at 16
    packages is 0.503 (Morton 0.833)."""
    path = os.path.join(os.path.dirname(__file__), "..", "data", "gali.txt")
    d = np.genfromtxt(path, delimiter=",", names=True)
    pos = np.c_[d["x"], d["y"], d["z"]]
    m = d["m"].astype(float)
    n, ps = len(m), 63
    perm = orb_order(pos, package_size=ps)
    label = np.empty(n, dtype=int)
    label[perm] = np.arange(n) // ps
    diff = pos[:, None, :] - pos[None, :, :]
    w = m[:, None] * m[None, :] / (np.einsum("ijk,ijk->ij", diff, diff) + 1e-3)
    np.fill_diagonal(w, 0.0)
    cut = w[label[:, None] != label[None, :]].sum() / w.sum()
    assert abs(cut - 0.503) < 1e-3


def test_morton_keys_leave_the_callers_array_alone():
    pos = np.array([[0.0, 1.0, np.nan], [1.0, 0.0, 0.0]])
    morton_keys(pos)
    assert np.isnan(pos[0, 2])
