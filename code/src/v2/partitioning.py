# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Package partitioning strategies for the distributed (V2) N-body flow.

A "package" is the set of particles a worker advances in one epoch. Every worker
computes forces from the *full* system state and only writes back its own
particles, so the partition is a pure work-assignment: any disjoint, covering
split of the N bodies is *physically identical*. It only changes which particles
share a package -- i.e. which pairs are integrated together versus predicted
across the package boundary by the g-ladder background predictor.

The distributed flow slices packages as *contiguous index ranges* over the body
array (see ``epoch_tasks.build_epoch_tasks``: ``start=(pkg-1)*size``,
``end=pkg*size``). Rather than teach every seam (task payload, worker slice,
merge, GPU backend) about arbitrary membership lists, we permute the body array
**once** at job creation. Contiguous slicing of the permuted array then yields
the requested package shape with zero downstream changes:

- ``linear``  -- identity permutation (file order). Default.
- ``random``  -- seeded shuffle. Decorrelates the input file order and balances
  per-package work (galaxy ICs are listed by structure, so contiguous packages
  are otherwise imbalanced: a dense core in one, a sparse halo in another).
- ``spatial`` -- Morton / Z-order sort of the initial positions. Contiguous
  slices become compact spatial boxes, keeping interacting neighbours in the
  same package (fewer strongly-interacting pairs split across the predicted
  background boundary).
- ``orb``     -- orthogonal recursive bisection (Warren & Salmon 1993): split
  the bodies along the longest axis of their bounding box, recursively, with
  the cuts placed exactly on the package boundaries, so every package is one
  axis-aligned box. Unlike the Z-curve it has no jumps across the domain, so
  its locality does not degrade with the package count.

``idBody`` carries particle identity, so results stay identifiable regardless of
order. Only the per-body ``n`` ordinal is renumbered downstream (still unique).
A run is fully reproducible: ``random`` is determined by ``(strategy, seed)``,
``spatial`` and ``orb`` by the initial positions (and, for ``orb``, the package
size).

This is a static, permute-once scheme -- the ordering is fixed for the whole run.
A spatial ordering computed at t=0 slowly decays as particles drift. Refreshing
it every epoch would require per-package membership lists (a larger change to the
task/merge seams) and is intentionally out of scope here.
"""

import math
from typing import Dict, List, Optional

import numpy as np


# Strategy identifiers (canonical source of truth, imported by the API validator).
LINEAR = "linear"
RANDOM = "random"
SPATIAL = "spatial"
ORB = "orb"
VALID_PARTITION_STRATEGIES = (LINEAR, RANDOM, SPATIAL, ORB)

DEFAULT_PARTITION_STRATEGY = LINEAR
DEFAULT_PARTITION_SEED = 0

# Bits per axis for the 3D Morton key. 3 * 21 = 63 bits, so the interleaved key
# fits in a uint64. 21 bits => 2**21 (~2.1M) buckets per axis, far finer than any
# N we run, so quantisation never limits the ordering.
_MORTON_BITS = 21
_MORTON_SCALE = np.float64((1 << _MORTON_BITS) - 1)


def _spread_bits_21(v: np.ndarray) -> np.ndarray:
    """Spread the low 21 bits of each uint64 so there are two 0-gaps between them.

    Standard 3D-Morton "magic bits" dilation: bit i moves to position 3*i, which
    lets three spread coordinates be OR'd together into an interleaved Z-order
    key. Vectorised over a uint64 array.
    """
    x = v.astype(np.uint64) & np.uint64(0x1FFFFF)
    x = (x | (x << np.uint64(32))) & np.uint64(0x1F00000000FFFF)
    x = (x | (x << np.uint64(16))) & np.uint64(0x1F0000FF0000FF)
    x = (x | (x << np.uint64(8))) & np.uint64(0x100F00F00F00F00F)
    x = (x | (x << np.uint64(4))) & np.uint64(0x10C30C30C30C30C3)
    x = (x | (x << np.uint64(2))) & np.uint64(0x1249249249249249)
    return x


def morton_keys(positions: np.ndarray) -> np.ndarray:
    """(N,) uint64 63-bit Z-order (Morton) keys of the (N, 3) positions.

    Positions are quantised into a ``2**_MORTON_BITS`` grid spanning the system's
    bounding box, then the per-axis buckets are bit-interleaved. Sorting by this
    key visits space along a Z-order curve, so bodies adjacent in the sorted
    order are (almost always) spatially close."""
    pos = np.nan_to_num(np.asarray(positions, dtype=np.float64))  # a copy: never the caller's array
    lo = pos.min(axis=0)
    hi = pos.max(axis=0)
    span = hi - lo
    span[span == 0.0] = 1.0  # collapse degenerate axes to a single bucket

    scaled = (pos - lo) / span * _MORTON_SCALE
    q = np.clip(np.floor(scaled), 0.0, _MORTON_SCALE).astype(np.uint64)  # (N, 3)

    return (
        _spread_bits_21(q[:, 0])
        | (_spread_bits_21(q[:, 1]) << np.uint64(1))
        | (_spread_bits_21(q[:, 2]) << np.uint64(2))
    )


def orb_order(positions: np.ndarray, package_size: Optional[int] = None) -> np.ndarray:
    """Orthogonal recursive bisection order of `positions`.

    Package sizes follow the pipeline's slicing: P = ceil(n / package_size)
    packages of package_size bodies, the last one shorter. Each recursion
    splits the current group of packages in half by COUNT (so the cut lands
    exactly on a package boundary), along the longest axis of the group's
    bounding box, using a stable sort. On ties np.argmax picks the lowest axis.
    Deterministic from the positions alone. Without package_size, every body is
    its own leaf, which gives a k-d ordering.
    """
    pos = np.nan_to_num(np.asarray(positions, dtype=np.float64))
    n = len(pos)
    if n == 0:
        return np.empty(0, dtype=np.int64)
    if package_size is None or package_size <= 0:
        sizes = [1] * n
    else:
        p = math.ceil(n / package_size)
        sizes = [package_size] * (p - 1) + [n - package_size * (p - 1)]
    out: List[np.ndarray] = []
    stack = [(np.arange(n, dtype=np.int64), sizes)]
    while stack:                       # explicit stack: depth-first, left before right
        idx, szs = stack.pop()
        if len(szs) == 1:
            out.append(idx)
            continue
        half = len(szs) // 2
        cut = int(sum(szs[:half]))
        sub = pos[idx]
        axis = int(np.argmax(sub.max(axis=0) - sub.min(axis=0)))
        idx = idx[np.argsort(sub[:, axis], kind="stable")]
        stack.append((idx[cut:], szs[half:]))
        stack.append((idx[:cut], szs[:half]))
    return np.concatenate(out)


def compute_permutation(
    strategy: str,
    bodies: List[Dict],
    seed: int = DEFAULT_PARTITION_SEED,
    package_size: Optional[int] = None,
) -> np.ndarray:
    """The (N,) int64 permutation of range(len(bodies)) that reshapes contiguous
    packages into `strategy` (one of VALID_PARTITION_STRATEGIES, ValueError
    otherwise). seed: for 'random'. package_size: so 'orb' cuts on the
    package boundaries. 'spatial' and 'orb' read the bodies' x, y, z."""
    n = len(bodies)
    strategy = (strategy or DEFAULT_PARTITION_STRATEGY).lower()

    if strategy == LINEAR:
        return np.arange(n, dtype=np.int64)

    if strategy == RANDOM:
        return np.random.default_rng(seed).permutation(n).astype(np.int64)

    if strategy == SPATIAL:
        positions = np.array(
            [(b["x"], b["y"], b["z"]) for b in bodies], dtype=np.float64
        )
        # Stable sort so bodies sharing a Morton cell keep their input order,
        # which keeps `spatial` fully deterministic from the ICs.
        return np.argsort(morton_keys(positions), kind="stable").astype(np.int64)

    if strategy == ORB:
        positions = np.array(
            [(b["x"], b["y"], b["z"]) for b in bodies], dtype=np.float64
        ).reshape(-1, 3)
        return orb_order(positions, package_size)

    raise ValueError(
        f"Unknown partition strategy '{strategy}'. "
        f"Must be one of: {', '.join(VALID_PARTITION_STRATEGIES)}"
    )


def apply_partition(
    bodies: List[Dict],
    strategy: str = DEFAULT_PARTITION_STRATEGY,
    seed: int = DEFAULT_PARTITION_SEED,
    package_size: Optional[int] = None,
) -> List[Dict]:
    """Reorder ``bodies`` once so contiguous package slicing yields `strategy`.

    ``linear`` returns the list unchanged. Other strategies return a new list of
    the same body dicts (references, not copies) in the permuted order. The body
    dicts are never mutated, so the physical system is untouched -- only the
    package boundaries move.
    """
    strategy = (strategy or DEFAULT_PARTITION_STRATEGY).lower()
    if strategy == LINEAR:
        return bodies
    perm = compute_permutation(strategy, bodies, seed, package_size)
    return [bodies[int(i)] for i in perm]
