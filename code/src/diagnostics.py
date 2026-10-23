# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""In-loop physics diagnostics: finite-state checks, conserved quantities, and
center-of-mass tools, cheap enough to run at snapshot boundaries, so a run can
abort instead of producing NaN snapshots and record its own energy, momentum
and centre-of-mass drift (the stepping kernels themselves use fastmath and no
guards).

Nothing here uses fastmath (it lets the compiler ASSUME finite values,
defeating the NaN guard, and reassociate sums) except potential_energy_fast,
the comparison service's copy of potential_energy.

Softened potential matches physics.compute_accelerations and REBOUND
(sqrt(r^2 + eps^2)), so energies are directly comparable across all three.
"""
from __future__ import annotations

from typing import Tuple

import numpy as np
from numba import jit, prange

from constants import G_CONST, SOFTENING_SQ


@jit(nopython=True, nogil=True, cache=True)
def is_finite_state(positions: np.ndarray, velocities: np.ndarray) -> bool:
    """True iff every position and velocity component is finite (no NaN/Inf).

    O(N), no fastmath (which would let the compiler assume finiteness). Meant to
    run once per snapshot to catch a blown-up integration before it is persisted.
    """
    n = positions.shape[0]
    for i in range(n):
        for d in range(3):
            if not np.isfinite(positions[i, d]):
                return False
            if not np.isfinite(velocities[i, d]):
                return False
    return True


@jit(nopython=True, nogil=True, cache=True)
def kinetic_energy(velocities: np.ndarray, masses: np.ndarray) -> float:
    """Total kinetic energy, 0.5 * sum(m v^2)."""
    kinetic = 0.0
    for i in range(len(masses)):
        kinetic += 0.5 * masses[i] * (
            velocities[i, 0] * velocities[i, 0] + velocities[i, 1] * velocities[i, 1]
            + velocities[i, 2] * velocities[i, 2]
        )
    return kinetic


@jit(nopython=True, nogil=True, cache=True)
def _pair_potential(px, py, pz, masses, i, softening_sq, G) -> float:
    """Body i's share of the potential: the pairs (i, j > i)."""
    s = 0.0
    mi = masses[i]
    xi, yi, zi = px[i], py[i], pz[i]
    for j in range(i + 1, len(masses)):
        dx = px[j] - xi
        dy = py[j] - yi
        dz = pz[j] - zi
        dist = np.sqrt(dx * dx + dy * dy + dz * dz + softening_sq)
        s -= G * mi * masses[j] / dist
    return s


@jit(nopython=True, nogil=True, cache=True)
def potential_energy(
    positions: np.ndarray,
    masses: np.ndarray,
    softening_sq: float = SOFTENING_SQ,
    G: float = G_CONST,
) -> float:
    """Softened Newtonian potential, the O(N^2) sum over unique pairs i<j.

    Single-threaded. Sums exactly as total_energy does (each body's pairs,
    then the bodies in order), so both give the same bits.
    """
    px = np.ascontiguousarray(positions[:, 0])
    py = np.ascontiguousarray(positions[:, 1])
    pz = np.ascontiguousarray(positions[:, 2])
    potential = 0.0
    for i in range(len(masses)):
        potential += _pair_potential(px, py, pz, masses, i, softening_sq, G)
    return potential


@jit(nopython=True, nogil=True, cache=True, fastmath=True)
def potential_energy_fast(
    positions: np.ndarray,
    masses: np.ndarray,
    softening_sq: float = SOFTENING_SQ,
    G: float = G_CONST,
) -> float:
    """potential_energy under fastmath, its pair loop inlined so that it
    vectorises: about 8% faster at N = 20,001, equal to it to ~1e-13
    relative. Single-threaded, for the comparison service, which runs several
    computations at once on their own threads (numba's default workqueue
    threading layer is not safe for concurrent parallel-kernel calls)."""
    px = np.ascontiguousarray(positions[:, 0])
    py = np.ascontiguousarray(positions[:, 1])
    pz = np.ascontiguousarray(positions[:, 2])
    potential = 0.0
    for i in range(len(masses)):
        s = 0.0
        mi = masses[i]
        xi, yi, zi = px[i], py[i], pz[i]
        for j in range(i + 1, len(masses)):
            dx = px[j] - xi
            dy = py[j] - yi
            dz = pz[j] - zi
            s -= G * mi * masses[j] / np.sqrt(dx * dx + dy * dy + dz * dz + softening_sq)
        potential += s
    return potential


@jit(nopython=True, nogil=True, cache=True, parallel=True)
def total_energy(
    positions: np.ndarray,
    velocities: np.ndarray,
    masses: np.ndarray,
    softening_sq: float = SOFTENING_SQ,
    G: float = G_CONST,
) -> Tuple[float, float]:
    """Return (kinetic, potential), the potential parallelised over bodies.

    The same sums as kinetic_energy and potential_energy, bit for bit: each
    thread computes whole per-body partials (written only to partial[i]),
    summed afterwards in body order. Intended for snapshot-cadence use, not
    every step.
    """
    n = len(masses)
    px = np.ascontiguousarray(positions[:, 0])
    py = np.ascontiguousarray(positions[:, 1])
    pz = np.ascontiguousarray(positions[:, 2])
    partial = np.zeros(n, dtype=np.float64)
    for i in prange(n):
        partial[i] = _pair_potential(px, py, pz, masses, i, softening_sq, G)
    potential = 0.0
    for i in range(n):
        potential += partial[i]
    return kinetic_energy(velocities, masses), potential


@jit(nopython=True, nogil=True, cache=True)
def total_momentum(velocities: np.ndarray, masses: np.ndarray) -> np.ndarray:
    p = np.zeros(3, dtype=np.float64)
    for i in range(len(masses)):
        m = masses[i]
        p[0] += m * velocities[i, 0]
        p[1] += m * velocities[i, 1]
        p[2] += m * velocities[i, 2]
    return p


@jit(nopython=True, nogil=True, cache=True)
def total_angular_momentum(positions: np.ndarray, velocities: np.ndarray,
                           masses: np.ndarray) -> np.ndarray:
    L = np.zeros(3, dtype=np.float64)
    for i in range(len(masses)):
        m = masses[i]
        x, y, z = positions[i, 0], positions[i, 1], positions[i, 2]
        vx, vy, vz = velocities[i, 0], velocities[i, 1], velocities[i, 2]
        L[0] += m * (y * vz - z * vy)
        L[1] += m * (z * vx - x * vz)
        L[2] += m * (x * vy - y * vx)
    return L


# --- Center of mass: pure numpy. Not hot enough to justify numba --------------

def center_of_mass(positions: np.ndarray, velocities: np.ndarray,
                   masses: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Return (com_position, com_velocity), each shape (3,)."""
    total = masses.sum()
    com_pos = (masses[:, None] * positions).sum(axis=0) / total
    com_vel = (masses[:, None] * velocities).sum(axis=0) / total
    return com_pos, com_vel


def com_drift(positions: np.ndarray, velocities: np.ndarray,
              masses: np.ndarray) -> Tuple[float, float]:
    """Return (|com_position|, |com_velocity|) — the COM offset magnitudes, a
    diagnostic for round-off-induced drift over a run (should stay ~0)."""
    com_pos, com_vel = center_of_mass(positions, velocities, masses)
    return float(np.linalg.norm(com_pos)), float(np.linalg.norm(com_vel))
