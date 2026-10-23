# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Shared Physics Module for N-Body Simulation.

This module provides the core physics calculations used by all workers
(distributed, monolithic, GPU, V2). It centralizes:
1. Force/acceleration computation (O(N^2) direct method)
2. Integration with force calculation (combined step functions), one stepper
   per integrator for both force algorithms: direct summation here, the
   Barnes-Hut O(N log N) tree in barnes_hut.py

GPU kernels remain in physics_gpu.py as they require PyCUDA.
"""

from typing import Tuple

import numpy as np
from numba import jit, prange

from barnes_hut import compute_accelerations_barnes_hut, compute_package_accelerations_barnes_hut
from constants import (
    DEFAULT_THETA,
    FR_C1,
    FR_C2,
    FR_D1,
    FR_D2,
    FR_TAU1,
    FR_TAU2,
    FR_TAU3,
    G_CONST,
    SOFTENING_SQ,
)
from package_sources import _global_indices_for_ids, _stage_source_positions


# Core Force Calculation

@jit(nopython=True, nogil=True, cache=True, parallel=True, fastmath=True)
def compute_accelerations(
    positions: np.ndarray,
    masses: np.ndarray,
    ids: np.ndarray,
    softening_sq: float = SOFTENING_SQ
) -> np.ndarray:
    """(N, 3) accelerations of all N bodies by O(N^2) direct summation, the
    core physics calculation shared by all CPU workers, with Plummer softening.
    ids exclude self (and duplicate-ID) interactions.

    The inner loop is branchless: the same-ID exclusion (which also covers
    i == j self-interaction) is a select that zeroes the force term instead
    of a `continue`, so the loop still SIMD-vectorizes. fastmath enables
    FMA + reciprocal-sqrt vectorization of the sqrt/divide."""
    n = len(masses)
    accelerations = np.zeros((n, 3), dtype=np.float64)

    # SoA copies: contiguous per-coordinate arrays vectorize cleanly,
    # unlike strided (N, 3) row access. One O(N) copy vs the O(N^2) main loop.
    px = np.ascontiguousarray(positions[:, 0])
    py = np.ascontiguousarray(positions[:, 1])
    pz = np.ascontiguousarray(positions[:, 2])

    # Outer body loop parallelized via prange. Each iteration writes only
    # to accelerations[i, :], inputs are read-only — no race conditions.
    # NUMBA_NUM_THREADS env var controls the thread count (set per pod via manifest).
    for i in prange(n):
        xi = px[i]
        yi = py[i]
        zi = pz[i]
        my_id = ids[i]
        ax = 0.0
        ay = 0.0
        az = 0.0
        for j in range(n):
            # Displacement vector (from i to j)
            dx = px[j] - xi
            dy = py[j] - yi
            dz = pz[j] - zi

            # Plummer softening: r_eff^2 = r^2 + eps^2 (smooth, C-infinity)
            dist_sq = dx*dx + dy*dy + dz*dz + softening_sq
            inv_dist3 = 1.0 / (np.sqrt(dist_sq) * dist_sq)  # 1/r^3

            # Force magnitude. Zeroed for self/duplicate-ID pairs
            # (branchless select, keeps the loop vectorizable)
            force = G_CONST * masses[j] * inv_dist3
            if ids[j] == my_id:
                force = 0.0

            # Accumulate acceleration: a = F * r_hat = F * r / |r|
            ax += force * dx
            ay += force * dy
            az += force * dz

        accelerations[i, 0] = ax
        accelerations[i, 1] = ay
        accelerations[i, 2] = az

    return accelerations


@jit(nopython=True, nogil=True, cache=True, parallel=True, fastmath=True)
def compute_package_accelerations(
    k_positions: np.ndarray,
    k_ids: np.ndarray,
    all_positions: np.ndarray,
    all_masses: np.ndarray,
    all_ids: np.ndarray,
    softening_sq: float = SOFTENING_SQ
) -> np.ndarray:
    """(K, 3) accelerations of a package of K bodies (k_positions, k_ids) from
    ALL N bodies (the all_* arrays): a distributed worker computes only its
    package but needs the forces of every body.

    As in the monolithic kernel, self-interaction is excluded by ID, as a
    branchless select (force zeroed) so the loop still SIMD-vectorizes under
    fastmath. The multi-stage package steppers pass sources built by
    _stage_source_positions, which scatters each package body's current stage
    position into its own row, so the self-term's displacement is zero there. The
    ID exclusion keeps it out regardless (e.g. for callers passing epoch-start
    sources against stage-position targets)."""
    k = len(k_ids)
    n = len(all_masses)
    accelerations = np.zeros((k, 3), dtype=np.float64)

    # SoA copies of source coordinates for contiguous SIMD-friendly access
    px = np.ascontiguousarray(all_positions[:, 0])
    py = np.ascontiguousarray(all_positions[:, 1])
    pz = np.ascontiguousarray(all_positions[:, 2])

    # Outer package loop parallelized. Each i writes only to accelerations[i, :].
    for i in prange(k):
        xi = k_positions[i, 0]
        yi = k_positions[i, 1]
        zi = k_positions[i, 2]
        my_id = k_ids[i]
        ax = 0.0
        ay = 0.0
        az = 0.0
        for j in range(n):
            # Compute displacement vector (from i to j)
            dx = px[j] - xi
            dy = py[j] - yi
            dz = pz[j] - zi

            # Plummer softening: r_eff^2 = r^2 + eps^2
            dist_sq = dx*dx + dy*dy + dz*dz + softening_sq
            inv_dist3 = 1.0 / (np.sqrt(dist_sq) * dist_sq)

            # Force magnitude. Zeroed for the self-term (branchless select)
            force = G_CONST * all_masses[j] * inv_dist3
            if all_ids[j] == my_id:
                force = 0.0

            # Accumulate acceleration
            ax += force * dx
            ay += force * dy
            az += force * dz

        accelerations[i, 0] = ax
        accelerations[i, 1] = ay
        accelerations[i, 2] = az

    return accelerations


# Force algorithm dispatch
# Every stepper takes the force algorithm as an argument (algorithm, theta): one
# implementation per integrator for direct summation and Barnes-Hut alike.

DIRECT = 0
BARNES_HUT = 1


def force_algorithm(name: str) -> int:
    """The steppers' algorithm code for a job's force_algorithm ('direct' or 'barnes_hut')."""
    if name == 'barnes_hut':
        return BARNES_HUT
    if name == 'direct':
        return DIRECT
    raise ValueError(f"unknown force algorithm {name!r}")


@jit(nopython=True, nogil=True, cache=True)
def accelerations_of(
    positions: np.ndarray,
    masses: np.ndarray,
    ids: np.ndarray,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
    softening_sq: float = SOFTENING_SQ,
) -> np.ndarray:
    """Accelerations of all bodies: compute_accelerations or, for BARNES_HUT,
    compute_accelerations_barnes_hut at opening angle theta."""
    if algorithm == BARNES_HUT:
        return compute_accelerations_barnes_hut(positions, masses, ids, theta, G_CONST, softening_sq)
    return compute_accelerations(positions, masses, ids, softening_sq=softening_sq)


@jit(nopython=True, nogil=True, cache=True)
def package_accelerations_of(
    k_positions: np.ndarray,
    k_ids: np.ndarray,
    all_positions: np.ndarray,
    all_masses: np.ndarray,
    all_ids: np.ndarray,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
    softening_sq: float = SOFTENING_SQ,
) -> np.ndarray:
    """Accelerations of the K package bodies from all N source bodies:
    compute_package_accelerations or, for BARNES_HUT, its tree counterpart."""
    if algorithm == BARNES_HUT:
        return compute_package_accelerations_barnes_hut(
            k_positions, k_ids, all_positions, all_masses, all_ids, theta, G_CONST, softening_sq)
    return compute_package_accelerations(k_positions, k_ids, all_positions, all_masses, all_ids,
                                         softening_sq=softening_sq)


# Combined force + integration steps (for monolithic workers). Every stepper
# takes the (N, 3) positions and velocities, the (N,) masses and ids, the step
# dt, the softening and the force algorithm, and returns the new positions and
# velocities with the step's accelerations (its last force evaluation). The
# package steppers below take the package's K bodies (k_*) and ALL N bodies
# (all_*), and return the package's K rows.

@jit(nopython=True, nogil=True, cache=True)
def step_symplectic_euler(
    positions: np.ndarray,
    velocities: np.ndarray,
    masses: np.ndarray,
    ids: np.ndarray,
    time_step: float,
    softening_sq: float = SOFTENING_SQ,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Symplectic Euler (semi-implicit), better energy conservation for
    Hamiltonian systems:
        a_t = F(x_t) / m
        v_{t+1} = v_t + a_t * dt
        x_{t+1} = x_t + v_{t+1} * dt   (uses NEW velocity)"""
    accelerations = accelerations_of(positions, masses, ids, algorithm, theta, softening_sq)

    new_velocities = velocities + accelerations * time_step

    new_positions = positions + new_velocities * time_step

    return new_positions, new_velocities, accelerations


@jit(nopython=True, nogil=True, cache=True)
def step_simple_euler(
    positions: np.ndarray,
    velocities: np.ndarray,
    masses: np.ndarray,
    ids: np.ndarray,
    time_step: float,
    softening_sq: float = SOFTENING_SQ,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Simple (explicit) Euler:
        a_t = F(x_t) / m
        x_{t+1} = x_t + v_t * dt       (uses OLD velocity)
        v_{t+1} = v_t + a_t * dt"""
    accelerations = accelerations_of(positions, masses, ids, algorithm, theta, softening_sq)

    new_positions = positions + velocities * time_step

    new_velocities = velocities + accelerations * time_step

    return new_positions, new_velocities, accelerations


@jit(nopython=True, nogil=True, cache=True)
def step_direct(
    positions: np.ndarray,
    velocities: np.ndarray,
    masses: np.ndarray,
    ids: np.ndarray,
    time_step: float,
    use_symplectic: bool = True,
    softening_sq: float = SOFTENING_SQ,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One Euler step: symplectic if use_symplectic, else simple (explicit)."""
    if use_symplectic:
        return step_symplectic_euler(positions, velocities, masses, ids, time_step, softening_sq, algorithm, theta)
    else:
        return step_simple_euler(positions, velocities, masses, ids, time_step, softening_sq, algorithm, theta)


@jit(nopython=True, nogil=True, cache=True)
def step_velocity_verlet(
    positions: np.ndarray,
    velocities: np.ndarray,
    masses: np.ndarray,
    ids: np.ndarray,
    accelerations: np.ndarray,
    time_step: float,
    softening_sq: float = SOFTENING_SQ,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Velocity Verlet (Stormer-Verlet), second order O(dt^2) and symplectic:
        x_{t+1} = x_t + v_t * dt + 0.5 * a_t * dt^2
        a_{t+1} = F(x_{t+1}) / m
        v_{t+1} = v_t + 0.5 * (a_t + a_{t+1}) * dt

    accelerations are the previous step's (a_t). The returned a_{t+1} is the
    next step's."""
    dt_sq_half = 0.5 * time_step * time_step

    new_positions = positions + velocities * time_step + accelerations * dt_sq_half

    new_accelerations = accelerations_of(new_positions, masses, ids, algorithm, theta, softening_sq)

    new_velocities = velocities + 0.5 * (accelerations + new_accelerations) * time_step

    return new_positions, new_velocities, new_accelerations


@jit(nopython=True, nogil=True, cache=True)
def step_rk4(
    positions: np.ndarray,
    velocities: np.ndarray,
    masses: np.ndarray,
    ids: np.ndarray,
    time_step: float,
    softening_sq: float = SOFTENING_SQ,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """RK4 (4th-order Runge-Kutta), O(dt^4), 4 force evaluations per step. Not
    symplectic, so energy drifts over very long simulations.
        k1_v = a(x)                   k1_x = v
        k2_v = a(x + 0.5*dt*k1_x)     k2_x = v + 0.5*dt*k1_v
        k3_v = a(x + 0.5*dt*k2_x)     k3_x = v + 0.5*dt*k2_v
        k4_v = a(x + dt*k3_x)         k4_x = v + dt*k3_v
        x_new = x + dt/6 * (k1_x + 2*k2_x + 2*k3_x + k4_x)
        v_new = v + dt/6 * (k1_v + 2*k2_v + 2*k3_v + k4_v)
    The accelerations returned are k4_v."""
    dt = time_step
    dt_half = 0.5 * dt

    # k1
    k1_v = accelerations_of(positions, masses, ids, algorithm, theta, softening_sq)
    k1_x = velocities.copy()

    # k2
    pos_k2 = positions + dt_half * k1_x
    k2_v = accelerations_of(pos_k2, masses, ids, algorithm, theta, softening_sq)
    k2_x = velocities + dt_half * k1_v

    # k3
    pos_k3 = positions + dt_half * k2_x
    k3_v = accelerations_of(pos_k3, masses, ids, algorithm, theta, softening_sq)
    k3_x = velocities + dt_half * k2_v

    # k4
    pos_k4 = positions + dt * k3_x
    k4_v = accelerations_of(pos_k4, masses, ids, algorithm, theta, softening_sq)
    k4_x = velocities + dt * k3_v

    # Final update
    dt_sixth = dt / 6.0
    new_positions = positions + dt_sixth * (k1_x + 2.0 * k2_x + 2.0 * k3_x + k4_x)
    new_velocities = velocities + dt_sixth * (k1_v + 2.0 * k2_v + 2.0 * k3_v + k4_v)

    # Return last-stage accelerations for adaptive dt estimation (no extra force eval).
    # k4_v is at a nearby position — close enough for the softening-length dt estimate.
    return new_positions, new_velocities, k4_v


@jit(nopython=True, nogil=True, cache=True)
def step_forest_ruth(
    positions: np.ndarray,
    velocities: np.ndarray,
    masses: np.ndarray,
    ids: np.ndarray,
    time_step: float,
    softening_sq: float = SOFTENING_SQ,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Forest-Ruth, 4th-order symplectic: triple Verlet composition with
    Yoshida coefficients (constants.FR_*), chi = 1 / (2 - 2^(1/3)) ~ 1.3512,
    3 force evaluations:
        Drift(chi/2 * dt) -> Kick(chi * dt) ->
        Drift((1 - chi)/2 * dt) -> Kick((1 - 2*chi) * dt) ->
        Drift((1 - chi)/2 * dt) -> Kick(chi * dt) ->
        Drift(chi/2 * dt)

    At a FIXED dt the energy error stays bounded and oscillates without secular
    drift. Under adaptive dt (step size changed from the state) the map is no
    longer symplectic and that guarantee does not hold. The accelerations
    returned are the last kick's."""
    c1 = FR_C1
    c2 = FR_C2
    d1 = FR_D1
    d2 = FR_D2
    dt = time_step

    pos1 = positions + c1 * dt * velocities
    a1 = accelerations_of(pos1, masses, ids, algorithm, theta, softening_sq)
    vel1 = velocities + d1 * dt * a1

    pos2 = pos1 + c2 * dt * vel1
    a2 = accelerations_of(pos2, masses, ids, algorithm, theta, softening_sq)
    vel2 = vel1 + d2 * dt * a2

    pos3 = pos2 + c2 * dt * vel2
    a3 = accelerations_of(pos3, masses, ids, algorithm, theta, softening_sq)
    vel3 = vel2 + d1 * dt * a3

    new_positions = pos3 + c1 * dt * vel3

    return new_positions, vel3, a3


# Adaptive Timestepping

@jit(nopython=True, nogil=True, cache=True)
def estimate_adaptive_dt(
    accelerations: np.ndarray,
    current_dt: float,
    eta: float = 0.02,
    softening: float = 3.16e-5,
    dt_min: float = 1e-10,
    dt_max: float = 0.1,
) -> float:
    """The next timestep by the softening-length criterion (GADGET-style,
    Power et al. 2003), clamped to [dt_min, dt_max]:

    dt_new = eta * sqrt(softening / a_max)

    When bodies are close, a_max spikes and dt shrinks automatically. When
    bodies are far apart, a_max is small and dt grows. softening is the
    softening length (the square root of softening_sq). current_dt is unused."""
    a_max_sq = 0.0
    for i in range(len(accelerations)):
        a_sq = (accelerations[i, 0] ** 2 +
                accelerations[i, 1] ** 2 +
                accelerations[i, 2] ** 2)
        if a_sq != a_sq:
            return dt_min   # NaN: a diverged state gets the smallest step, not the largest
        if a_sq > a_max_sq:
            a_max_sq = a_sq
    # Near-zero acceleration guard: 1e-60 chosen well below any physical acceleration
    # to avoid sqrt(0) while not interfering with legitimate small values.
    if a_max_sq < 1e-60:
        return dt_max
    a_max = np.sqrt(a_max_sq)
    dt_new = eta * np.sqrt(softening / a_max)
    return max(dt_min, min(dt_max, dt_new))


@jit(nopython=True, nogil=True, cache=True)
def estimate_adaptive_dt_from_amax(
    a_max: float,
    current_dt: float,
    eta: float = 0.02,
    softening: float = 3.16e-5,
    dt_min: float = 1e-10,
    dt_max: float = 0.1,
) -> float:
    """estimate_adaptive_dt from a pre-computed a_max: distributed workers
    aggregate it from the packages at the merge point. current_dt is unused."""
    if a_max != a_max:
        return dt_min   # NaN: a diverged state gets the smallest step, not the largest
    if a_max < 1e-30:
        return dt_max
    dt_new = eta * np.sqrt(softening / a_max)
    return max(dt_min, min(dt_max, dt_new))


# Combined Force + Integration Steps (for distributed package workers)

@jit(nopython=True, nogil=True, cache=True)
def step_package_symplectic_euler(
    k_positions: np.ndarray,
    k_velocities: np.ndarray,
    k_masses: np.ndarray,
    k_ids: np.ndarray,
    all_positions: np.ndarray,
    all_masses: np.ndarray,
    all_ids: np.ndarray,
    time_step: float,
    softening_sq: float = SOFTENING_SQ,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One symplectic Euler step for a package of K bodies, its forces from all
    N bodies (forces at the epoch start only, so no predictor is involved)."""
    accelerations = package_accelerations_of(k_positions, k_ids, all_positions, all_masses, all_ids,
                                             algorithm, theta, softening_sq)

    new_velocities = k_velocities + accelerations * time_step

    new_positions = k_positions + new_velocities * time_step

    return new_positions, new_velocities, accelerations


@jit(nopython=True, nogil=True, cache=True)
def step_package_simple_euler(
    k_positions: np.ndarray,
    k_velocities: np.ndarray,
    k_masses: np.ndarray,
    k_ids: np.ndarray,
    all_positions: np.ndarray,
    all_masses: np.ndarray,
    all_ids: np.ndarray,
    time_step: float,
    softening_sq: float = SOFTENING_SQ,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One simple (explicit) Euler step for a package of K bodies, its forces
    from all N bodies."""
    accelerations = package_accelerations_of(k_positions, k_ids, all_positions, all_masses, all_ids,
                                             algorithm, theta, softening_sq)

    new_positions = k_positions + k_velocities * time_step

    new_velocities = k_velocities + accelerations * time_step

    return new_positions, new_velocities, accelerations


@jit(nopython=True, nogil=True, cache=True)
def step_package_direct(
    k_positions: np.ndarray,
    k_velocities: np.ndarray,
    k_masses: np.ndarray,
    k_ids: np.ndarray,
    all_positions: np.ndarray,
    all_masses: np.ndarray,
    all_ids: np.ndarray,
    time_step: float,
    use_symplectic: bool = True,
    softening_sq: float = SOFTENING_SQ,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One package Euler step: symplectic if use_symplectic, else simple."""
    if use_symplectic:
        return step_package_symplectic_euler(k_positions, k_velocities, k_masses, k_ids, all_positions,
                                             all_masses, all_ids, time_step, softening_sq, algorithm, theta)
    else:
        return step_package_simple_euler(k_positions, k_velocities, k_masses, k_ids, all_positions,
                                         all_masses, all_ids, time_step, softening_sq, algorithm, theta)


@jit(nopython=True, nogil=True, cache=True)
def step_package_velocity_verlet(
    k_positions: np.ndarray,
    k_velocities: np.ndarray,
    k_masses: np.ndarray,
    k_ids: np.ndarray,
    k_accelerations: np.ndarray,
    all_positions: np.ndarray,
    all_velocities: np.ndarray,
    all_accelerations: np.ndarray,
    all_masses: np.ndarray,
    all_ids: np.ndarray,
    time_step: float,
    predictor_order: int = 1,
    softening_sq: float = SOFTENING_SQ,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Velocity Verlet for a package of K bodies (the k_* arrays), its forces
    from ALL N bodies (the all_* arrays, at the epoch start).

    k_accelerations are the package's accelerations at the epoch start. k_masses
    is unused (the masses are read from all_masses). all_accelerations are read
    only when predictor_order >= 2 (may be zeros otherwise).

    The second force evaluation (at the new package positions, tau = dt) uses a
    background predicted to the stage time, with the package's own new positions
    scattered in (_stage_source_positions explains the predictor orders). VV is
    2nd order, so predictor_order 1 already saturates it. The accel term changes
    the numbers but not the order."""
    dt_sq_half = 0.5 * time_step * time_step

    # Drift (step 1) for the K package bodies
    new_k_positions = k_positions + k_velocities * time_step + k_accelerations * dt_sq_half

    # Sources at the step end (tau = dt): the predicted background with the
    # package's drifted positions scattered in
    global_indices = _global_indices_for_ids(k_ids, all_ids)
    src_positions = _stage_source_positions(all_positions, all_velocities, all_accelerations, time_step,
                                            predictor_order, new_k_positions, global_indices)
    new_k_accelerations = package_accelerations_of(new_k_positions, k_ids, src_positions, all_masses, all_ids,
                                                   algorithm, theta, softening_sq)

    # Kick (step 3)
    new_k_velocities = k_velocities + 0.5 * (k_accelerations + new_k_accelerations) * time_step

    return new_k_positions, new_k_velocities, new_k_accelerations


@jit(nopython=True, nogil=True, cache=True)
def step_package_rk4(
    k_positions: np.ndarray,
    k_velocities: np.ndarray,
    k_masses: np.ndarray,
    k_ids: np.ndarray,
    all_positions: np.ndarray,
    all_velocities: np.ndarray,
    all_accelerations: np.ndarray,
    all_masses: np.ndarray,
    all_ids: np.ndarray,
    time_step: float,
    predictor_order: int = 1,
    softening_sq: float = SOFTENING_SQ,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """RK4 for a package of K bodies (the k_* arrays), its forces from ALL N
    bodies (the all_* arrays, at the epoch start), 4 force evaluations.
    all_accelerations are read only when predictor_order >= 2 (may be zeros
    otherwise).

    Each stage evaluates forces against a background predicted to the stage time
    (tau = 0, dt/2, dt/2, dt for k1..k4), with the package's own stage positions
    scattered in. _stage_source_positions explains the predictor orders and the
    global order they allow. The accelerations returned are k4_v, as step_rk4's."""
    dt = time_step
    dt_half = 0.5 * dt
    global_indices = _global_indices_for_ids(k_ids, all_ids)

    # k1 (stage time tau = 0)
    src1 = _stage_source_positions(all_positions, all_velocities, all_accelerations, 0.0, predictor_order,
                                   k_positions, global_indices)
    k1_v = package_accelerations_of(k_positions, k_ids, src1, all_masses, all_ids, algorithm, theta, softening_sq)
    k1_x = k_velocities.copy()

    # k2 (tau = dt/2)
    pos_k2 = k_positions + dt_half * k1_x
    src2 = _stage_source_positions(all_positions, all_velocities, all_accelerations, dt_half, predictor_order,
                                   pos_k2, global_indices)
    k2_v = package_accelerations_of(pos_k2, k_ids, src2, all_masses, all_ids, algorithm, theta, softening_sq)
    k2_x = k_velocities + dt_half * k1_v

    # k3 (tau = dt/2)
    pos_k3 = k_positions + dt_half * k2_x
    src3 = _stage_source_positions(all_positions, all_velocities, all_accelerations, dt_half, predictor_order,
                                   pos_k3, global_indices)
    k3_v = package_accelerations_of(pos_k3, k_ids, src3, all_masses, all_ids, algorithm, theta, softening_sq)
    k3_x = k_velocities + dt_half * k2_v

    # k4 (tau = dt)
    pos_k4 = k_positions + dt * k3_x
    src4 = _stage_source_positions(all_positions, all_velocities, all_accelerations, dt, predictor_order,
                                   pos_k4, global_indices)
    k4_v = package_accelerations_of(pos_k4, k_ids, src4, all_masses, all_ids, algorithm, theta, softening_sq)
    k4_x = k_velocities + dt * k3_v

    # Final update
    dt_sixth = dt / 6.0
    new_k_positions = k_positions + dt_sixth * (k1_x + 2.0 * k2_x + 2.0 * k3_x + k4_x)
    new_k_velocities = k_velocities + dt_sixth * (k1_v + 2.0 * k2_v + 2.0 * k3_v + k4_v)

    return new_k_positions, new_k_velocities, k4_v


@jit(nopython=True, nogil=True, cache=True)
def step_package_forest_ruth(
    k_positions: np.ndarray,
    k_velocities: np.ndarray,
    k_masses: np.ndarray,
    k_ids: np.ndarray,
    all_positions: np.ndarray,
    all_velocities: np.ndarray,
    all_accelerations: np.ndarray,
    all_masses: np.ndarray,
    all_ids: np.ndarray,
    time_step: float,
    predictor_order: int = 1,
    softening_sq: float = SOFTENING_SQ,
    algorithm: int = DIRECT,
    theta: float = DEFAULT_THETA,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Forest-Ruth for a package of K bodies (the k_* arrays), its forces from
    ALL N bodies (the all_* arrays, at the epoch start), 3 force evaluations.
    all_accelerations are read only when predictor_order >= 2 (may be zeros
    otherwise).

    Each kick evaluates forces against a background predicted to the cumulative
    drift time of that kick (tau = c1*dt, (c1+c2)*dt, (c1+2*c2)*dt), with the
    package's own stage positions scattered in. _stage_source_positions explains
    the predictor orders and the global order they allow. The accelerations
    returned are the last kick's (a3), as step_forest_ruth's."""
    c1 = FR_C1
    c2 = FR_C2
    d1 = FR_D1
    d2 = FR_D2
    dt = time_step
    global_indices = _global_indices_for_ids(k_ids, all_ids)

    pos1 = k_positions + c1 * dt * k_velocities
    src1 = _stage_source_positions(all_positions, all_velocities, all_accelerations, FR_TAU1 * dt,
                                   predictor_order, pos1, global_indices)
    a1 = package_accelerations_of(pos1, k_ids, src1, all_masses, all_ids, algorithm, theta, softening_sq)
    vel1 = k_velocities + d1 * dt * a1

    pos2 = pos1 + c2 * dt * vel1
    src2 = _stage_source_positions(all_positions, all_velocities, all_accelerations, FR_TAU2 * dt,
                                   predictor_order, pos2, global_indices)
    a2 = package_accelerations_of(pos2, k_ids, src2, all_masses, all_ids, algorithm, theta, softening_sq)
    vel2 = vel1 + d2 * dt * a2

    pos3 = pos2 + c2 * dt * vel2
    src3 = _stage_source_positions(all_positions, all_velocities, all_accelerations, FR_TAU3 * dt,
                                   predictor_order, pos3, global_indices)
    a3 = package_accelerations_of(pos3, k_ids, src3, all_masses, all_ids, algorithm, theta, softening_sq)
    vel3 = vel2 + d1 * dt * a3

    new_k_positions = pos3 + c1 * dt * vel3

    return new_k_positions, vel3, a3


# JIT Warmup Function

def warmup_physics_jit():
    """
    Warm up JIT compilation of the kernels and steppers, for both force
    algorithms.

    Call this at worker startup to avoid first-job latency.
    """
    # ids are int32, as bodies_to_numpy() in worker_utils.py makes them
    n = 10
    positions = np.random.randn(n, 3).astype(np.float64)
    velocities = np.random.randn(n, 3).astype(np.float64)
    masses = np.abs(np.random.randn(n).astype(np.float64)) + 0.1
    ids = np.arange(n, dtype=np.int32)
    time_step = 0.001
    # passed explicitly, as the backends do: an omitted argument compiles a
    # separate signature that production calls never use
    eps2 = float(SOFTENING_SQ)
    theta = float(DEFAULT_THETA)
    k = 3
    k_positions = positions[:k].copy()
    k_velocities = velocities[:k].copy()
    k_masses = masses[:k].copy()
    k_ids = ids[:k].copy()

    accelerations = compute_accelerations(positions, masses, ids, softening_sq=eps2)
    for algorithm in (DIRECT, BARNES_HUT):
        accelerations = accelerations_of(positions, masses, ids, algorithm, theta, eps2)
        k_accelerations = package_accelerations_of(k_positions, k_ids, positions, masses, ids, algorithm, theta, eps2)
        for symplectic in (True, False):
            _ = step_direct(positions, velocities, masses, ids, time_step, symplectic, eps2, algorithm, theta)
            _ = step_package_direct(k_positions, k_velocities, k_masses, k_ids, positions, masses, ids,
                                    time_step, symplectic, eps2, algorithm, theta)
        _ = step_velocity_verlet(positions, velocities, masses, ids, accelerations, time_step, eps2, algorithm, theta)
        _ = step_rk4(positions, velocities, masses, ids, time_step, eps2, algorithm, theta)
        _ = step_forest_ruth(positions, velocities, masses, ids, time_step, eps2, algorithm, theta)
        for order in (1, 2):
            _ = step_package_velocity_verlet(k_positions, k_velocities, k_masses, k_ids, k_accelerations,
                                             positions, velocities, accelerations, masses, ids, time_step,
                                             order, eps2, algorithm, theta)
            _ = step_package_rk4(k_positions, k_velocities, k_masses, k_ids, positions, velocities,
                                 accelerations, masses, ids, time_step, order, eps2, algorithm, theta)
            _ = step_package_forest_ruth(k_positions, k_velocities, k_masses, k_ids, positions, velocities,
                                         accelerations, masses, ids, time_step, order, eps2, algorithm, theta)

    # Warm up adaptive timestepping functions
    _ = estimate_adaptive_dt(accelerations, 0.001, eta=0.02, softening=1e-2, dt_min=1e-10, dt_max=0.01)
    _ = estimate_adaptive_dt_from_amax(5.0, 0.001, eta=0.02, softening=1e-2, dt_min=1e-10, dt_max=0.01)
