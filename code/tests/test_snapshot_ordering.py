# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Tests for canonical idBody ordering of analysis snapshots.

This guards the fix for cross-job comparison: two jobs of the same physical
system but different body row-order (e.g. a V2 'spatial' job vs a 'linear' one)
must compare body-for-body, which requires reordering both into idBody order
before the positional (row-index) comparison the analysis layer performs.
"""

import numpy as np
import pytest

from utils.snapshot_ordering import canonicalize_by_id


def _system(n=50, seed=0):
    """A random cloud with unique idBody per body (ids NOT pre-sorted)."""
    rng = np.random.default_rng(seed)
    ids = rng.permutation(n).astype(np.int64)          # scrambled identities
    positions = rng.uniform(-5, 5, size=(n, 3))
    velocities = rng.uniform(-1, 1, size=(n, 3))
    masses = rng.uniform(0.5, 2.0, size=n)
    return positions, velocities, masses, ids


# ---------------------------------------------------------------------------
# Core contract: output is sorted by idBody, rows stay consistent
# ---------------------------------------------------------------------------

def test_orders_by_id_and_keeps_rows_together():
    p, v, m, ids = _system(40, seed=1)
    cp, cv, cm = canonicalize_by_id(p, v, m, ids)

    order = np.argsort(ids, kind="stable")
    assert np.array_equal(cp, p[order])
    assert np.array_equal(cv, v[order])
    assert np.array_equal(cm, m[order])
    # ids would now be ascending (we don't return them, but the row map proves it)
    assert np.all(np.diff(ids[order]) > 0)


def test_none_ids_is_untouched():
    p, v, m, _ = _system(10)
    cp, cv, cm = canonicalize_by_id(p, v, m, None)
    assert cp is p and cv is v and cm is m  # no reorder, no copy


def test_already_sorted_is_noop_without_copy():
    p, v, m, _ = _system(30)
    ids = np.arange(30, dtype=np.int64)  # already ascending
    cp, cv, cm = canonicalize_by_id(p, v, m, ids)
    # Fast-path returns the very same arrays (no argsort/fancy-index copy).
    assert cp is p and cv is v and cm is m


@pytest.mark.parametrize("n", [0, 1, 2])
def test_tiny_inputs(n):
    p, v, m, ids = _system(n)
    cp, cv, cm = canonicalize_by_id(p, v, m, ids)
    assert cp.shape == (n, 3) and cm.shape == (n,)


# ---------------------------------------------------------------------------
# Cross-job comparison by row index
# ---------------------------------------------------------------------------

def _row_rmse(pa, pb):
    return float(np.sqrt(np.mean(np.sum((pa - pb) ** 2, axis=1))))


def test_canonicalization_fixes_cross_job_alignment():
    """A 'linear' job and a permuted ('spatial') job of the SAME state must read
    as identical after canonicalisation, and as garbage without it."""
    n = 200
    rng = np.random.default_rng(7)
    ids = np.arange(n, dtype=np.int64)
    positions = rng.uniform(-10, 10, size=(n, 3))
    velocities = rng.uniform(-1, 1, size=(n, 3))
    masses = rng.uniform(0.5, 2.0, size=n)

    # Job A: file/idBody order (linear/monolithic).
    a = (positions.copy(), velocities.copy(), masses.copy(), ids.copy())

    # Job B: same bodies, permuted rows (what a 'spatial'/'random' V2 job emits).
    perm = rng.permutation(n)
    b = (positions[perm], velocities[perm], masses[perm], ids[perm])

    # A naive row-index comparison is badly misaligned.
    assert _row_rmse(a[0], b[0]) > 1.0

    ca_p, _, _ = canonicalize_by_id(*a)
    cb_p, _, _ = canonicalize_by_id(*b)

    # After canonicalising both, identical bodies line up -> zero divergence.
    assert _row_rmse(ca_p, cb_p) == pytest.approx(0.0, abs=1e-12)


def test_two_different_permutations_align():
    """random-seed-A vs random-seed-B (two different orders) also reconcile."""
    n = 128
    rng = np.random.default_rng(3)
    ids = np.arange(n, dtype=np.int64)
    positions = rng.uniform(-10, 10, size=(n, 3))
    vel = np.zeros((n, 3))
    mass = np.ones(n)

    pa = rng.permutation(n)
    pb = rng.permutation(n)
    ca, _, _ = canonicalize_by_id(positions[pa], vel[pa], mass[pa], ids[pa])
    cb, _, _ = canonicalize_by_id(positions[pb], vel[pb], mass[pb], ids[pb])
    assert _row_rmse(ca, cb) == pytest.approx(0.0, abs=1e-12)
