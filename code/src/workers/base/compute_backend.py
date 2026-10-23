# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
ComputeBackend Protocol - Abstracts CPU vs GPU physics computation.

This protocol defines the interface that both CPU and GPU backends must implement,
enabling workers to be agnostic about the underlying compute hardware.
"""

import logging
import os
from abc import ABC, abstractmethod
from typing import Optional, Tuple

import numpy as np

from constants import SOFTENING_SQ


class ComputeBackend(ABC):
    """Abstract interface for physics computation (CPU or GPU)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend identifier (e.g., 'cpu', 'gpu')."""
        pass

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend is available on the current system."""
        pass

    @abstractmethod
    def warmup(self) -> None:
        """
        Perform any warmup needed (e.g., JIT compilation, GPU initialization).
        Should be called once before processing begins.
        """
        pass

    @abstractmethod
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
        """One step of ALL bodies (monolithic workers): (new_positions,
        new_velocities, accelerations). algorithm: 'direct' or 'barnes_hut',
        integration_method: an integrators.IntegrationMethod value,
        previous_accelerations: the previous step's (velocity Verlet needs them)."""
        pass

    @abstractmethod
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
        """One step of a PACKAGE of K bodies (k_*), its forces from ALL N bodies
        (all_*, at the epoch start): the distributed workers' step. Returns
        (new_k_positions, new_k_velocities, new_k_accelerations).

        Multi-stage integrators (velocity_verlet, rk4, forest_ruth and their
        g-named variants) predict the non-package "background" bodies to each
        stage time with a Taylor predictor whose order the method name selects:
        frozen (x), linear (x + tau*v, the plain names) or accel
        (x + tau*v + 0.5*tau^2*a0, needing all_accelerations). The predictor order
        bounds the global order of the distributed step (frozen 1, linear 2,
        accel 3, see package_sources._stage_source_positions). Euler methods only
        evaluate at tau=0 and ignore it. all_accelerations, if omitted, are
        recomputed locally when the method needs them.

        The accelerations returned are the package's last-stage ones, for adaptive
        timestep heuristics without an extra force evaluation: a(x_old) for Euler
        methods, a(x_new) for VV, the last stage's for RK4/FR."""
        pass

    def cleanup(self) -> None:
        """
        Clean up any resources (e.g., GPU memory).
        Called when worker shuts down. Default implementation does nothing.
        """
        pass

    # Optional resident-state API (monolithic jobs)
    #
    # Backends that keep simulation state in device memory (GPU) can avoid
    # a full host<->device round-trip per epoch: the worker loads state once,
    # steps in place, and fetches state back only when it actually needs it
    # (snapshots, final result). Backends without device state (CPU) leave
    # supports_resident_state False and the worker uses compute_step().

    @property
    def supports_resident_state(self) -> bool:
        """Whether this backend can hold simulation state between steps."""
        return False

    def load_resident_state(
        self,
        positions: np.ndarray,
        velocities: np.ndarray,
        masses: np.ndarray,
        ids: np.ndarray,
        time_step: float,
        integration_method: str = 'symplectic_euler',
        softening_sq: float = SOFTENING_SQ,
    ) -> None:
        """Load simulation state into backend-resident memory."""
        raise NotImplementedError(f"{self.name} backend has no resident state")

    def step_resident(
        self,
        time_step: float,
        need_accelerations: bool = False,
    ) -> Optional[np.ndarray]:
        """Advance the resident state by one step of time_step (which may change
        every step under adaptive dt). need_accelerations: return the last-stage
        accelerations as an (N, 3) float64 array (a small transfer, for adaptive
        dt), else None with nothing transferred."""
        raise NotImplementedError(f"{self.name} backend has no resident state")

    def fetch_resident_state(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(positions, velocities, accelerations) of the resident state, on the
        host. The accelerations are the last stage's of the most recent step: only
        for velocity Verlet a(x_new), the exact previous accelerations a resumed VV
        run needs, for the other methods an approximate current acceleration (fine
        for adaptive dt)."""
        raise NotImplementedError(f"{self.name} backend has no resident state")


class ComputeError(Exception):
    """Raised when a compute backend encounters an error.

    restart_worker: the backend is unusable in this process (a CUDA fault
    leaves the context broken), so after handing the task back the worker
    exits and its pod restarts it. Such an error is recoverable: the task is
    retried, counted, by a worker with a working device."""

    def __init__(self, backend: str, message: str, recoverable: bool = True, restart_worker: bool = False):
        self.backend = backend
        self.recoverable = recoverable or restart_worker
        self.restart_worker = restart_worker
        super().__init__(f"[{backend}] {message}")


def exit_for_restart(reason: str) -> None:
    """End the process at once (no cleanup touches the broken device), for the
    pod to restart it."""
    logging.getLogger(__name__).critical(f"Exiting for a restart: {reason}")
    for handler in logging.getLogger().handlers:
        handler.flush()
    os._exit(1)
