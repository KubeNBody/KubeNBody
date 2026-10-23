# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Barnes-Hut Algorithm for O(N log N) N-body force calculation.

This module provides Numba JIT-compiled functions for:
1. Octree construction
2. Center of mass computation
3. Force calculation with opening angle criterion (MAC)

References:
    Barnes, J., & Hut, P. (1986). "A hierarchical O(N log N) force-calculation algorithm."
    Nature, 324(6096), 446-449.
"""

from typing import Tuple

import numpy as np
from numba import jit, prange

from constants import (
    DEFAULT_THETA,
    G_CONST,
    MAX_TREE_DEPTH,
    SOFTENING_SQ,
)
from package_sources import _global_indices_for_ids


# OCTREE NODE DATA STRUCTURE

# Octree nodes as a flat structured array (Numba cannot JIT objects)
OCTREE_NODE_DTYPE = np.dtype([
    # Cell geometry
    ('center_x', np.float64),      # Cell center X
    ('center_y', np.float64),      # Cell center Y
    ('center_z', np.float64),      # Cell center Z
    ('size', np.float64),          # Cell edge length

    # Aggregate properties
    ('mass', np.float64),          # Total mass in cell
    ('com_x', np.float64),         # Center of mass X
    ('com_y', np.float64),         # Center of mass Y
    ('com_z', np.float64),         # Center of mass Z

    # Tree structure
    ('is_leaf', np.int32),         # 1 if leaf, 0 if internal
    ('body_index', np.int32),      # Body index if leaf. -1 empty, -2 several bodies
    ('count', np.int32),           # Bodies in this leaf (>1 only when aggregated)
    ('first_child', np.int32),     # Index of first child (-1 if leaf)
])


# OCTREE CONSTRUCTION

@jit(nopython=True, nogil=True, cache=True)
def compute_bounding_box(positions: np.ndarray) -> Tuple[float, float, float, float]:
    """(center_x, center_y, center_z, size) of the cube containing all bodies."""
    min_x = np.min(positions[:, 0])
    max_x = np.max(positions[:, 0])
    min_y = np.min(positions[:, 1])
    max_y = np.max(positions[:, 1])
    min_z = np.min(positions[:, 2])
    max_z = np.max(positions[:, 2])

    # Use largest dimension for cubic box
    size = max(max_x - min_x, max_y - min_y, max_z - min_z)
    size = size * 1.01 + 1e-10  # Small padding to ensure all bodies inside

    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0
    center_z = (min_z + max_z) / 2.0

    return center_x, center_y, center_z, size


@jit(nopython=True, nogil=True, cache=True)
def get_octant(px: float, py: float, pz: float,
               cx: float, cy: float, cz: float) -> int:
    """The octant (0-7) of position (px, py, pz) in the cell centred at
    (cx, cy, cz): (x_bit << 0) | (y_bit << 1) | (z_bit << 2), each bit 1 if the
    coordinate is >= the centre's."""
    octant = 0
    if px >= cx:
        octant |= 1
    if py >= cy:
        octant |= 2
    if pz >= cz:
        octant |= 4
    return octant


@jit(nopython=True, nogil=True, cache=True)
def _grow_octree(octree: np.ndarray, num_nodes: int) -> np.ndarray:
    """Return a copy of the node array with twice the capacity."""
    new = np.zeros(2 * octree.shape[0], dtype=octree.dtype)
    for i in range(num_nodes):
        new[i] = octree[i]
    return new


@jit(nopython=True, nogil=True, cache=True)
def _insert_body(octree: np.ndarray, num_nodes: int,
                 px: float, py: float, pz: float,
                 mass: float, body_idx: int, max_depth: int):
    """Insert body body_idx into the octree, by iterative descent (no recursion
    in Numba). Returns (octree, num_nodes): the array is grown when full.

    A body that reaches an occupied leaf at max_depth (nearly coincident
    bodies), or an occupied leaf whose centre of mass is exactly at the body's
    position (coincident bodies, at any depth), is aggregated into that leaf,
    whose mass and centre of mass then cover all its bodies (body_index = -2).
    With Plummer softening the force between bodies in such a cell (edge < root
    size / 2**max_depth) is negligible, so this matches direct summation
    (rather than leaving the body out of the tree)."""
    node_idx = 0  # Start at root
    depth = 0

    while True:
        node = octree[node_idx]

        if node['is_leaf'] == 1:
            if node['body_index'] == -1:
                # Empty leaf - place body here
                octree[node_idx]['body_index'] = body_idx
                octree[node_idx]['count'] = 1
                octree[node_idx]['mass'] = mass
                octree[node_idx]['com_x'] = px
                octree[node_idx]['com_y'] = py
                octree[node_idx]['com_z'] = pz
                return octree, num_nodes

            if node['com_x'] == px and node['com_y'] == py and node['com_z'] == pz:
                # Exactly coincident with the leaf's (centre of) mass: no split
                # can ever separate them, so aggregate now rather than
                # subdividing down all max_depth levels. Mass added at the COM
                # leaves the COM unchanged (and keeps it bit-exact, so further
                # coincident bodies keep matching).
                octree[node_idx]['mass'] = node['mass'] + mass
                octree[node_idx]['count'] = node['count'] + 1
                octree[node_idx]['body_index'] = -2
                return octree, num_nodes

            if depth >= max_depth:
                # Deepest level: aggregate instead of subdividing further
                m_old = node['mass']
                m_new = m_old + mass
                if m_new > 0.0:
                    octree[node_idx]['com_x'] = (m_old * node['com_x'] + mass * px) / m_new
                    octree[node_idx]['com_y'] = (m_old * node['com_y'] + mass * py) / m_new
                    octree[node_idx]['com_z'] = (m_old * node['com_z'] + mass * pz) / m_new
                octree[node_idx]['mass'] = m_new
                octree[node_idx]['count'] = node['count'] + 1
                octree[node_idx]['body_index'] = -2
                return octree, num_nodes

            # Occupied leaf - subdivide
            if num_nodes + 8 > octree.shape[0]:
                octree = _grow_octree(octree, num_nodes)
                node = octree[node_idx]

            # Save old body info before converting to internal node
            old_body_idx = node['body_index']
            old_count = node['count']
            old_mass = node['mass']
            old_com_x = node['com_x']
            old_com_y = node['com_y']
            old_com_z = node['com_z']

            first_child = num_nodes

            octree[node_idx]['first_child'] = first_child
            octree[node_idx]['is_leaf'] = 0
            octree[node_idx]['body_index'] = -1
            octree[node_idx]['count'] = 0

            half_size = node['size'] / 2.0
            quarter_size = half_size / 2.0

            for c in range(8):
                child_idx = first_child + c

                # Compute child center based on octant
                offset_x = quarter_size if (c & 1) else -quarter_size
                offset_y = quarter_size if (c & 2) else -quarter_size
                offset_z = quarter_size if (c & 4) else -quarter_size

                octree[child_idx]['center_x'] = node['center_x'] + offset_x
                octree[child_idx]['center_y'] = node['center_y'] + offset_y
                octree[child_idx]['center_z'] = node['center_z'] + offset_z
                octree[child_idx]['size'] = half_size
                octree[child_idx]['is_leaf'] = 1
                octree[child_idx]['body_index'] = -1
                octree[child_idx]['count'] = 0
                octree[child_idx]['first_child'] = -1
                octree[child_idx]['mass'] = 0.0
                octree[child_idx]['com_x'] = 0.0
                octree[child_idx]['com_y'] = 0.0
                octree[child_idx]['com_z'] = 0.0

            num_nodes += 8

            # Re-insert old body into appropriate child
            old_octant = get_octant(old_com_x, old_com_y, old_com_z,
                                    node['center_x'], node['center_y'], node['center_z'])
            old_child_idx = first_child + old_octant
            octree[old_child_idx]['body_index'] = old_body_idx
            octree[old_child_idx]['count'] = old_count
            octree[old_child_idx]['mass'] = old_mass
            octree[old_child_idx]['com_x'] = old_com_x
            octree[old_child_idx]['com_y'] = old_com_y
            octree[old_child_idx]['com_z'] = old_com_z

        # Descend into the child containing the new body
        octant = get_octant(px, py, pz,
                            octree[node_idx]['center_x'],
                            octree[node_idx]['center_y'],
                            octree[node_idx]['center_z'])
        node_idx = octree[node_idx]['first_child'] + octant
        depth += 1


@jit(nopython=True, nogil=True, cache=True)
def _compute_centers_of_mass(octree: np.ndarray, num_nodes: int) -> None:
    """The centre of mass of every internal node, bottom-up: nodes in reverse
    order, so children come before their parents."""
    for node_idx in range(num_nodes - 1, -1, -1):
        node = octree[node_idx]

        if node['is_leaf'] == 0:  # Internal node
            total_mass = 0.0
            com_x, com_y, com_z = 0.0, 0.0, 0.0
            first_child = node['first_child']

            for c in range(8):
                child_idx = first_child + c
                if child_idx < num_nodes:
                    child = octree[child_idx]
                    if child['mass'] > 0:
                        total_mass += child['mass']
                        com_x += child['mass'] * child['com_x']
                        com_y += child['mass'] * child['com_y']
                        com_z += child['mass'] * child['com_z']

            if total_mass > 0:
                octree[node_idx]['mass'] = total_mass
                octree[node_idx]['com_x'] = com_x / total_mass
                octree[node_idx]['com_y'] = com_y / total_mass
                octree[node_idx]['com_z'] = com_z / total_mass


@jit(nopython=True, nogil=True, cache=True)
def build_octree(positions: np.ndarray, masses: np.ndarray,
                 max_depth: int = MAX_TREE_DEPTH) -> Tuple[np.ndarray, int]:
    """(octree_nodes, num_nodes) of the bodies, built by iterative insertion."""
    n = len(masses)
    # The tree walk's stack is sized for MAX_TREE_DEPTH. Never build deeper.
    max_depth = min(max_depth, MAX_TREE_DEPTH)

    # Initial capacity: trees take 3.9-5 nodes per body (Plummer 3.9, the disc
    # model data/gali.txt 4.9). _insert_body doubles it if clustered bodies
    # need more
    octree = np.zeros(5 * n + 16, dtype=OCTREE_NODE_DTYPE)

    cx, cy, cz, size = compute_bounding_box(positions)
    octree[0]['center_x'] = cx
    octree[0]['center_y'] = cy
    octree[0]['center_z'] = cz
    octree[0]['size'] = size
    octree[0]['is_leaf'] = 1
    octree[0]['body_index'] = -1
    octree[0]['count'] = 0
    octree[0]['first_child'] = -1
    octree[0]['mass'] = 0.0
    octree[0]['com_x'] = 0.0
    octree[0]['com_y'] = 0.0
    octree[0]['com_z'] = 0.0

    num_nodes = 1

    # Insert each body
    for i in range(n):
        octree, num_nodes = _insert_body(
            octree, num_nodes,
            positions[i, 0], positions[i, 1], positions[i, 2],
            masses[i], i, max_depth
        )

    _compute_centers_of_mass(octree, num_nodes)

    return octree[:num_nodes], num_nodes


# FORCE CALCULATION

@jit(nopython=True, nogil=True, cache=True)
def _compute_force_on_body(
    octree: np.ndarray,
    num_nodes: int,
    px: float, py: float, pz: float,
    body_idx: int,
    theta: float,
    G: float,
    softening_sq: float = SOFTENING_SQ
) -> Tuple[float, float, float]:
    """(ax, ay, az) on the body body_idx at (px, py, pz), by an iterative tree walk
    with an explicit stack.

    The Multipole Acceptance Criterion (MAC) is:
        s/d < theta
    where s is the cell size and d the unsoftened distance to the cell's COM
    (the force of an accepted cell uses the softened distance). If this
    criterion is satisfied, we approximate the cell as a single mass.

    Self-exclusion: a leaf whose body_index is body_idx is skipped, and an
    internal cell whose box contains the target is always opened, so the
    target's own mass never enters its force through a monopole. (A body inside
    a cell of edge s lies at most sqrt(3)*s from the cell's COM, so the MAC can
    accept such a cell only for theta > 1/sqrt(3) ~ 0.577: below that the
    containment check changes nothing.)"""
    ax, ay, az = 0.0, 0.0, 0.0

    # Iterative traversal using an explicit stack. Each level pushes at most
    # 8 children and pops one, so depth-first occupancy stays below
    # 7 * depth + 1. Size it for the deepest tree build_octree can make
    # (numba does not bounds-check the writes).
    stack = np.zeros(8 * MAX_TREE_DEPTH + 8, dtype=np.int32)
    stack_ptr = 0
    stack[stack_ptr] = 0  # Start at root
    stack_ptr += 1

    while stack_ptr > 0:
        stack_ptr -= 1
        node_idx = stack[stack_ptr]

        if node_idx >= num_nodes:
            continue

        node = octree[node_idx]

        # Skip empty nodes
        if node['mass'] == 0.0:
            continue

        # Skip self-interaction (leaf node containing this body)
        if node['is_leaf'] == 1 and node['body_index'] == body_idx:
            continue

        dx = node['com_x'] - px
        dy = node['com_y'] - py
        dz = node['com_z'] - pz
        raw_dist_sq = dx*dx + dy*dy + dz*dz
        # An aggregated leaf exactly at this body (it and its coincident
        # partners) exerts no force. Skip it so eps^2 = 0 cannot divide by 0.
        if node['is_leaf'] == 1 and raw_dist_sq == 0.0:
            continue

        # Plummer softening: r_eff^2 = r^2 + eps^2
        dist_sq = raw_dist_sq + softening_sq
        dist = np.sqrt(dist_sq)

        # Barnes-Hut criterion s/r < θ on the UNSOFTENED distance r (as
        # s^2 < θ^2 r^2): the softened distance would open fewer cells as eps
        # grows (median force error ~2% at eps^2 = 1, theta = 0.5), while this
        # keeps the error set by theta alone. The force stays softened.
        # A cell containing the target (closed box) is never accepted
        half = 0.5 * node['size']
        contains_target = (abs(px - node['center_x']) <= half and abs(py - node['center_y']) <= half
                           and abs(pz - node['center_z']) <= half)
        if node['is_leaf'] == 1 or (not contains_target
                                    and node['size'] * node['size'] < theta * theta * raw_dist_sq):
            # Accept approximation - treat as single mass
            inv_dist3 = 1.0 / (dist * dist_sq)  # 1/r^3 = 1/(r * r^2)
            force = G * node['mass'] * inv_dist3
            ax += force * dx
            ay += force * dy
            az += force * dz
        else:
            # Reject - descend into children
            first_child = node['first_child']
            if first_child >= 0:
                for c in range(8):
                    child_idx = first_child + c
                    if child_idx < num_nodes and octree[child_idx]['mass'] > 0:
                        stack[stack_ptr] = child_idx
                        stack_ptr += 1

    return ax, ay, az


@jit(nopython=True, nogil=True, cache=True)
def _spatial_order(positions: np.ndarray) -> np.ndarray:
    """Morton (Z-order) permutation of the target bodies.

    The parallel force loops visit targets in this order rather than in input
    order, so neighbouring targets, whose tree walks touch the same nodes, are
    handled by the same thread back to back (better cache use: about 1.7x at
    N = 2e5). Each target's walk and output row are independent of the order,
    so the accelerations are bitwise identical either way.
    """
    n = positions.shape[0]
    order = np.arange(n)
    if n < 2:
        return order
    lo = np.empty(3)
    span = 0.0
    for d in range(3):
        mn = positions[0, d]
        mx = positions[0, d]
        for i in range(1, n):
            v = positions[i, d]
            if v < mn:
                mn = v
            if v > mx:
                mx = v
        lo[d] = mn
        if mx - mn > span:
            span = mx - mn
    if span <= 0.0:
        return order
    scale = 2097151.0 / span                  # 21 bits per axis
    keys = np.empty(n, dtype=np.uint64)
    for i in range(n):
        key = np.uint64(0)
        for d in range(3):
            c = np.uint64(min(2097151.0, (positions[i, d] - lo[d]) * scale))
            # spread the 21 bits of c to every third bit
            c = (c | (c << np.uint64(32))) & np.uint64(0x1F00000000FFFF)
            c = (c | (c << np.uint64(16))) & np.uint64(0x1F0000FF0000FF)
            c = (c | (c << np.uint64(8))) & np.uint64(0x100F00F00F00F00F)
            c = (c | (c << np.uint64(4))) & np.uint64(0x10C30C30C30C30C3)
            c = (c | (c << np.uint64(2))) & np.uint64(0x1249249249249249)
            key |= c << np.uint64(d)
        keys[i] = key
    return np.argsort(keys)


@jit(nopython=True, nogil=True, cache=True, parallel=True)
def _forces_on_targets(
    octree: np.ndarray,
    num_nodes: int,
    targets: np.ndarray,
    self_index: np.ndarray,
    theta: float,
    G: float,
    softening_sq: float,
) -> np.ndarray:
    """Accelerations of the target positions from the tree. self_index[i] is
    target i's body index in the tree (its own leaf is skipped, -1: none).
    Targets are walked in Morton order, so consecutive threads walk nearby
    parts of the tree. The tree is read-only and each iteration writes its
    own row, so prange is race-free."""
    k = targets.shape[0]
    accelerations = np.zeros((k, 3), dtype=np.float64)
    order = _spatial_order(targets)
    for t in prange(k):
        i = order[t]
        ax, ay, az = _compute_force_on_body(
            octree, num_nodes,
            targets[i, 0], targets[i, 1], targets[i, 2],
            self_index[i], theta, G, softening_sq
        )
        accelerations[i, 0] = ax
        accelerations[i, 1] = ay
        accelerations[i, 2] = az
    return accelerations


@jit(nopython=True, nogil=True, cache=True, parallel=True)
def compute_accelerations_barnes_hut(
    positions: np.ndarray,
    masses: np.ndarray,
    ids: np.ndarray,
    theta: float = DEFAULT_THETA,
    G: float = G_CONST,
    softening_sq: float = SOFTENING_SQ
) -> np.ndarray:
    """(N, 3) accelerations of all N bodies by Barnes-Hut, the monolithic
    entry point. ids is unused (self-interaction is excluded by body index), kept
    for signature symmetry with the direct kernel."""
    n = len(masses)

    if n == 0:
        return np.zeros((0, 3), dtype=np.float64)

    octree, num_nodes = build_octree(positions, masses)
    # each body's own leaf is its index in the tree's body set
    return _forces_on_targets(octree, num_nodes, positions, np.arange(n), theta, G, softening_sq)


# PACKAGE-BASED CALCULATION (for distributed workers)

@jit(nopython=True, nogil=True, cache=True, parallel=True)
def compute_package_accelerations_barnes_hut(
    k_positions: np.ndarray,
    k_ids: np.ndarray,
    all_positions: np.ndarray,
    all_masses: np.ndarray,
    all_ids: np.ndarray,
    theta: float = DEFAULT_THETA,
    G: float = G_CONST,
    softening_sq: float = SOFTENING_SQ
) -> np.ndarray:
    """(K, 3) accelerations of a package of K bodies (k_*) from ALL N bodies
    (all_*, the octree's), the Barnes-Hut compute_package_accelerations: every
    force evaluation of the package steppers, and the initial accelerations of
    Barnes-Hut velocity Verlet."""
    k = len(k_ids)
    n = len(all_masses)

    if k == 0:
        return np.zeros((0, 3), dtype=np.float64)
    if n == 0:
        return np.zeros((k, 3), dtype=np.float64)

    octree, num_nodes = build_octree(all_positions, all_masses)

    # Global indices for self-interaction checks (one sorted lookup pass)
    global_indices = _global_indices_for_ids(k_ids, all_ids)
    return _forces_on_targets(octree, num_nodes, k_positions, global_indices, theta, G, softening_sq)


# UTILITY FUNCTIONS

def warmup_jit():
    """
    Warm up JIT compilation of the tree kernels with small test data (the
    steppers that use them are warmed up by physics.warmup_physics_jit).
    """
    # Small test case - use int32 for ids to match BODY_STRUCT in binary_format.py
    positions = np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]], dtype=np.float64)
    masses = np.array([1.0, 1.0], dtype=np.float64)
    ids = np.array([0, 1], dtype=np.int32)
    # passed explicitly, as the backends do: an omitted argument compiles a
    # separate signature that production calls never use
    eps2 = float(SOFTENING_SQ)

    _ = compute_accelerations_barnes_hut(positions, masses, ids, 0.5, G_CONST, eps2)
    _ = compute_package_accelerations_barnes_hut(positions, ids, positions, masses, ids, 0.5, G_CONST, eps2)
