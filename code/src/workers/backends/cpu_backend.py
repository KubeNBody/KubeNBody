# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""CPU compute backend: the Numba kernels of physics.py (direct, O(N^2)) and
barnes_hut.py (O(N log N))."""

import logging
from typing import Optional, Tuple

import numpy as np

from barnes_hut import warmup_jit
from constants import G_CONST, SOFTENING_SQ
from diagnostics import total_energy
from integrators import (
    PREDICTOR_ACCEL,
    IntegrationMethod,
    get_predictor_order,
    is_forest_ruth,
    is_rk4,
    is_velocity_verlet,
)
from physics import (
    BARNES_HUT,
    _global_indices_for_ids,
    accelerations_of,
    force_algorithm,
    package_accelerations_of,
    step_direct,
    step_forest_ruth,
    step_package_direct,
    step_package_forest_ruth,
    step_package_rk4,
    step_package_velocity_verlet,
    step_rk4,
    step_velocity_verlet,
    warmup_physics_jit,
)
from workers.base.compute_backend import ComputeBackend, ComputeError


logger = logging.getLogger(__name__)


def _check_integration_method(integration_method: str) -> None:
    """Raise ValueError for an unknown method name: the dispatch below would
    otherwise run it as simple Euler (the GPU engine raises likewise)."""
    if not IntegrationMethod.is_valid(integration_method):
        raise ValueError(
            f"Unknown integration_method '{integration_method}'. "
            f"Supported: {', '.join(m.value for m in IntegrationMethod)}"
        )


class CPUBackend(ComputeBackend):
    """CPU-based physics computation using Numba JIT."""

    @property
    def name(self) -> str:
        return 'cpu'

    @property
    def is_available(self) -> bool:
        return True  # CPU is always available

    def warmup(self) -> None:
        """Warmup Numba JIT compilation."""
        logger.info("Warming up CPU backend (JIT compilation)...")
        # direct summation too, so the first direct-summation epoch of a fresh
        # pod does not include compilation
        warmup_physics_jit()
        warmup_jit()
        # the snapshot diagnostics, with the call shape the workers use
        p = np.random.default_rng(0).standard_normal((4, 3))
        total_energy(p, p, np.ones(4), float(SOFTENING_SQ), float(G_CONST))
        logger.info("CPU backend warmup complete")

    def compute_step(
        self,
        positions: np.ndarray,
        velocities: np.ndarray,
        masses: np.ndarray,
        ids: np.ndarray,
        time_step: float,
        algorithm: str = 'direct',
        integration_method: str = 'symplectic_euler',
        barnes_hut_theta: float = 0.5,
        softening_sq: float = SOFTENING_SQ,
        previous_accelerations: Optional[np.ndarray] = None,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Compute one physics step for ALL bodies (monolithic workers).
        """
        _check_integration_method(integration_method)
        use_velocity_verlet = is_velocity_verlet(integration_method)
        use_rk4 = is_rk4(integration_method)
        use_forest_ruth = is_forest_ruth(integration_method)
        use_symplectic = integration_method == 'symplectic_euler'
        theta = self._validate_theta(barnes_hut_theta)
        code = force_algorithm(algorithm)

        try:
            if use_rk4:
                new_positions, new_velocities, new_accelerations = step_rk4(
                    positions, velocities, masses, ids, time_step, softening_sq, code, theta)
            elif use_forest_ruth:
                new_positions, new_velocities, new_accelerations = step_forest_ruth(
                    positions, velocities, masses, ids, time_step, softening_sq, code, theta)
            elif use_velocity_verlet:
                if previous_accelerations is None:
                    previous_accelerations = self._initial_accelerations(positions, masses, ids, code, theta,
                                                                         softening_sq)
                new_positions, new_velocities, new_accelerations = step_velocity_verlet(
                    positions, velocities, masses, ids, previous_accelerations, time_step, softening_sq, code, theta)
            else:
                new_positions, new_velocities, new_accelerations = step_direct(
                    positions, velocities, masses, ids, time_step, use_symplectic, softening_sq, code, theta)

            return new_positions, new_velocities, new_accelerations

        except RuntimeError as e:
            # a numba runtime error (e.g. a failed allocation)
            raise ComputeError(
                self.name,
                f"Physics computation failed: {e}",
                recoverable=False
            )

    def compute_package_step(
        self,
        k_positions: np.ndarray,
        k_velocities: np.ndarray,
        k_masses: np.ndarray,
        k_ids: np.ndarray,
        all_positions: np.ndarray,
        all_velocities: np.ndarray,
        all_masses: np.ndarray,
        all_ids: np.ndarray,
        time_step: float,
        algorithm: str = 'direct',
        integration_method: str = 'symplectic_euler',
        barnes_hut_theta: float = 0.5,
        softening_sq: float = SOFTENING_SQ,
        all_accelerations: Optional[np.ndarray] = None,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Compute one physics step for a PACKAGE of bodies (distributed workers).

        Returns (new_k_positions, new_k_velocities, new_k_accelerations).

        The background predictor order is resolved from `integration_method`
        (gvv_accel/grk4_accel/gfr_accel -> PREDICTOR_ACCEL, the others linear or
        frozen).
        For the accel predictor the epoch-start accelerations of ALL bodies are
        needed. If `all_accelerations` is not supplied they are recomputed locally
        here (an O(N^2) pass per step). The worker pipeline normally supplies
        them (returned by the previous epoch's tasks, or shipped by the merger).
        """
        _check_integration_method(integration_method)
        k_accelerations = None    # the package's a(0): taken from all_accelerations or computed
        use_velocity_verlet = is_velocity_verlet(integration_method)
        use_rk4 = is_rk4(integration_method)
        use_forest_ruth = is_forest_ruth(integration_method)
        use_symplectic = integration_method == 'symplectic_euler'
        theta = self._validate_theta(barnes_hut_theta)
        code = force_algorithm(algorithm)
        predictor_order = get_predictor_order(integration_method)

        # Background epoch-start accelerations for the accel predictor (order >= 2).
        # Multi-stage steppers always take an (N,3) all_accelerations array. It is
        # only read when predictor_order >= 2, so a zeros placeholder is fine for
        # frozen/linear. Recomputed locally when not supplied by the caller.
        if all_accelerations is None:
            if predictor_order >= PREDICTOR_ACCEL and len(all_masses) > 0:
                all_accelerations = accelerations_of(all_positions, all_masses, all_ids, code, theta, softening_sq)
            else:
                all_accelerations = np.zeros_like(all_positions)

        try:
            if use_rk4:
                new_k_positions, new_k_velocities, new_k_accel = step_package_rk4(
                    k_positions, k_velocities, k_masses, k_ids,
                    all_positions, all_velocities, all_accelerations, all_masses, all_ids,
                    time_step, predictor_order, softening_sq, code, theta)
            elif use_forest_ruth:
                new_k_positions, new_k_velocities, new_k_accel = step_package_forest_ruth(
                    k_positions, k_velocities, k_masses, k_ids,
                    all_positions, all_velocities, all_accelerations, all_masses, all_ids,
                    time_step, predictor_order, softening_sq, code, theta)
            elif use_velocity_verlet:
                # With the accel predictor the full-system epoch-start
                # accelerations are already at hand (shipped or recomputed
                # above). The package's own rows ARE its cached acceleration,
                # so the per-task local a0 evaluation is skipped.
                if predictor_order >= PREDICTOR_ACCEL:
                    rows = _global_indices_for_ids(k_ids, all_ids)
                    if (rows < 0).any():   # -1 (absent) would index the last row
                        raise ComputeError(self.name, "package body IDs missing from the "
                                           "full-system state", recoverable=False)
                    k_accelerations = np.ascontiguousarray(all_accelerations[rows])
                if k_accelerations is None:
                    k_accelerations = package_accelerations_of(k_positions, k_ids, all_positions, all_masses,
                                                               all_ids, code, theta, softening_sq)
                new_k_positions, new_k_velocities, new_k_accel = step_package_velocity_verlet(
                    k_positions, k_velocities, k_masses, k_ids, k_accelerations,
                    all_positions, all_velocities, all_accelerations, all_masses, all_ids,
                    time_step, predictor_order, softening_sq, code, theta)
            else:
                new_k_positions, new_k_velocities, new_k_accel = step_package_direct(
                    k_positions, k_velocities, k_masses, k_ids, all_positions, all_masses, all_ids,
                    time_step, use_symplectic, softening_sq, code, theta)

            return new_k_positions, new_k_velocities, new_k_accel

        except RuntimeError as e:
            raise ComputeError(
                self.name,
                f"Package physics computation failed: {e}",
                recoverable=False
            )

    def _validate_theta(self, theta: float) -> float:
        """Validate and clamp Barnes-Hut theta parameter."""
        if theta < 0.1 or theta > 2.0:
            logger.warning(f"barnes_hut_theta={theta} out of range [0.1, 2.0], clamping")
            return max(0.1, min(2.0, theta))
        return theta

    def _initial_accelerations(self, positions, masses, ids, code: int, theta: float,
                               softening_sq: float = SOFTENING_SQ) -> np.ndarray:
        """Velocity Verlet's accelerations before its first step. Barnes-Hut
        evaluates them with the package kernel over all bodies, which the
        recorded trajectories are built on."""
        if code == BARNES_HUT:
            return package_accelerations_of(positions, ids, positions, masses, ids, code, theta, softening_sq)
        return accelerations_of(positions, masses, ids, code, theta, softening_sq)
