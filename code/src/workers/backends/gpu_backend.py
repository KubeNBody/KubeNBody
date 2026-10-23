# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""GPU compute backend: the PyCUDA GPUNBodyEngine of physics_gpu.py, behind
the same interface as the CPU backend. Direct summation only (no Barnes-Hut
on the GPU)."""

import logging
import os
from typing import Dict, List, Optional, Tuple

import numpy as np

from constants import G_CONST, SOFTENING_SQ
from integrators import PREDICTOR_ACCEL, get_predictor_order, resolve_base_method
from physics import compute_accelerations
from workers.base.compute_backend import ComputeBackend, ComputeError


logger = logging.getLogger(__name__)

# Try to import GPU dependencies
try:
    from pycuda.driver import Error as CudaError

    from physics_gpu import GPUNBodyEngine, get_gpu_info, is_gpu_available
    GPU_AVAILABLE = is_gpu_available()
except ImportError:
    GPU_AVAILABLE = False
    CudaError = None
    GPUNBodyEngine = None

    def get_gpu_info():
        return None


def _compute_error(backend: str, what: str, e: Exception) -> ComputeError:
    """The ComputeError for a failure inside the engine. A CUDA driver error
    (an illegal address, a launch failure, out of memory) can leave the
    context unusable: the task is retried and the worker restarts. Anything
    else is the job's own fault."""
    if CudaError is not None and isinstance(e, CudaError):
        return ComputeError(backend, f"{what}: {e}", restart_worker=True)
    return ComputeError(backend, f"{what}: {e}", recoverable=False)


def _gpu_use_float32() -> bool:
    """float32 kernel mode, opt-in per deployment via NBODY_GPU_FLOAT32.

    Faster on GPUs with weak FP64 throughput (most consumer and many
    datacenter cards). Accuracy is job-dependent: keep float64 (the default)
    unless the energy-drift comparison for your system says otherwise.
    """
    return os.environ.get('NBODY_GPU_FLOAT32', '').lower() in ('1', 'true', 'yes')


class GPUBackend(ComputeBackend):
    """GPU-based physics computation using PyCUDA.

    NOTE: This backend requires PyCUDA and a CUDA-capable GPU.
    It does NOT support Barnes-Hut (a job requesting it fails with a ComputeError).
    """

    def __init__(self):
        self._engine: Optional['GPUNBodyEngine'] = None

    @property
    def name(self) -> str:
        return 'gpu'

    @property
    def is_available(self) -> bool:
        return GPU_AVAILABLE

    def warmup(self) -> None:
        """
        Verify GPU is available. Actual compilation happens on first use
        since we need the bodies to initialize the engine.
        """
        if not self.is_available:
            raise ComputeError(self.name, "GPU not available", recoverable=False)

        info = get_gpu_info()
        if info:
            logger.info(f"GPU backend warmup - Device: {info.get('name', 'Unknown')}")
        else:
            logger.info("GPU backend warmup - GPU available")

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

        `previous_accelerations` is accepted for interface compatibility but
        IGNORED: every call uploads the given state, which invalidates the
        engine's device-side Velocity Verlet acceleration cache, so VV
        re-evaluates a(x) on the device at the start of each call (one extra
        force evaluation per call). Workers that step many epochs on the GPU use
        the resident-state API instead, which keeps that cache across steps.
        """
        if not self.is_available:
            raise ComputeError(self.name, "GPU not available", recoverable=False)

        if algorithm == 'barnes_hut':
            raise ComputeError(
                self.name,
                "Barnes-Hut is not implemented on the GPU backend. "
                "Use algorithm='direct' with GPU, or switch to the CPU backend "
                "for Barnes-Hut.",
                recoverable=False,
            )

        # Initialize engine (or upload new state to the existing one) —
        # all transfers are vectorized numpy <-> structured-array, no dict loops
        self._ensure_engine_arrays(
            positions, velocities, masses, ids, time_step, integration_method,
            softening_sq=softening_sq,
        )

        try:
            # Run single step
            self._engine.simulate_single_step()

            new_positions, new_velocities = self._engine.get_state_arrays()

            # Last-stage accelerations from the GPU step (see engine docstring
            # for which stage per method). Matches the package path.
            new_accelerations = self._engine.get_last_accelerations()

            return new_positions, new_velocities, new_accelerations

        except Exception as e:
            raise _compute_error(self.name, "GPU computation failed", e) from e

    # Resident-state API: state stays on the GPU between epochs.
    # The monolithic worker uses this to avoid the per-epoch host<->device
    # round-trip entirely — transfers happen only on snapshot epochs.

    @property
    def supports_resident_state(self) -> bool:
        return GPU_AVAILABLE

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
        if not self.is_available:
            raise ComputeError(self.name, "GPU not available", recoverable=False)
        self._ensure_engine_arrays(
            positions, velocities, masses, ids, time_step, integration_method,
            softening_sq=softening_sq,
        )

    def step_resident(
        self,
        time_step: float,
        need_accelerations: bool = False,
    ) -> Optional[np.ndarray]:
        if self._engine is None:
            raise ComputeError(self.name, "load_resident_state() not called", recoverable=False)
        try:
            self._engine.set_time_step(time_step)
            self._engine.simulate_single_step()
            if need_accelerations:
                return self._engine.get_last_accelerations()
            return None
        except Exception as e:
            raise _compute_error(self.name, "GPU computation failed", e) from e

    def fetch_resident_state(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        if self._engine is None:
            raise ComputeError(self.name, "load_resident_state() not called", recoverable=False)
        try:
            positions, velocities = self._engine.get_state_arrays()
            accelerations = self._engine.get_last_accelerations()
            return positions, velocities, accelerations
        except Exception as e:
            raise _compute_error(self.name, "GPU state fetch failed", e) from e

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

        The GPU engine needs ALL bodies loaded, then computes physics for a subset.

        Returns (new_k_positions, new_k_velocities, new_k_accelerations). The
        accelerations are the last-stage values from the GPU step — close
        enough to current state to feed an adaptive-dt heuristic without
        triggering an extra force evaluation on the host.

        The GPU tile kernel implements the full predictor ladder (frozen / linear
        / accel), matching the CPU path. For the accel predictor (gvv_accel /
        grk4_accel / gfr_accel) the epoch-start accelerations a0 of ALL bodies are
        uploaded to the engine — shipped by the merger via `all_accelerations`, or
        recomputed locally here when absent (for gvv_accel the package's rows of
        a0 also serve as its old acceleration). Barnes-Hut is CPU-only.
        """
        if not self.is_available:
            raise ComputeError(self.name, "GPU not available", recoverable=False)

        if algorithm == 'barnes_hut':
            raise ComputeError(
                self.name,
                "Barnes-Hut is not implemented on the GPU backend. "
                "Use algorithm='direct' with GPU, or switch to the CPU backend "
                "for Barnes-Hut.",
                recoverable=False,
            )

        # Resolve the base integrator (so g-named variants dispatch) and the
        # background predictor order (0 frozen / 1 linear / 2 accel).
        predictor_order = get_predictor_order(integration_method)
        base_method = resolve_base_method(integration_method)

        # Find the package's position within all bodies (packages are
        # contiguous slices of the global body array)
        matches = np.nonzero(all_ids == k_ids[0])[0]
        if len(matches) == 0:
            raise ComputeError(self.name, "Package bodies not found in all_bodies", recoverable=False)
        start_idx = int(matches[0])

        package_size = len(k_ids)
        # The GPU kernels index the package as one slice of the global arrays.
        # Refuse any other membership instead of integrating the wrong bodies.
        if not np.array_equal(all_ids[start_idx:start_idx + package_size], k_ids):
            raise ComputeError(self.name, "Package bodies are not a contiguous slice of all_bodies",
                               recoverable=False)

        # Build full-state arrays: package rows carry their current state. The
        # non-package bodies keep their real velocities so the multi-stage
        # kernels can predict them to each stage time (x + tau*v, plus
        # 0.5*tau^2*a0 for the accel predictor) instead of freezing them.
        # The workers pass the package as a slice of the full state, so the
        # two full-system copies are made only when its rows differ (O(K)
        # check. update_bodies_from_arrays copies into its own buffer anyway).
        pkg = slice(start_idx, start_idx + package_size)
        positions_full, velocities_full = all_positions, all_velocities
        if not (np.array_equal(all_positions[pkg], k_positions)
                and np.array_equal(all_velocities[pkg], k_velocities)):
            positions_full = all_positions.copy()
            positions_full[pkg] = k_positions
            velocities_full = all_velocities.copy()
            velocities_full[pkg] = k_velocities

        # Distributed: body positions change per task but count/params stay the same
        self._ensure_engine_arrays(
            positions_full, velocities_full, all_masses, all_ids,
            time_step, base_method, softening_sq=softening_sq,
        )

        # Accel predictor (order >= 2): upload a0 for ALL bodies (same order as
        # all_positions / gpu_bodies). Prefer the merger-shipped array. Recompute
        # locally when absent (epoch 1, or a job that doesn't ship it).
        if predictor_order >= PREDICTOR_ACCEL:
            a0 = all_accelerations
            if a0 is None:
                a0 = compute_accelerations(
                    all_positions, all_masses, all_ids, softening_sq=softening_sq
                )
            self._engine.set_source_accelerations(a0)

        try:
            # Compute the package step. Only its K rows come back, as arrays
            return self._engine.simulate_package_step(
                start_idx, package_size, predictor_order=predictor_order
            )

        except Exception as e:
            raise _compute_error(self.name, "GPU package computation failed", e) from e

    def cleanup(self) -> None:
        """Release GPU resources."""
        if self._engine is not None:
            try:
                self._engine.cleanup()
            except Exception as e:
                logger.warning(f"Error during GPU cleanup: {e}")
            self._engine = None

    def _needs_engine_reinit(self, n_bodies: int, integration_method: str) -> bool:
        """Whether the engine must be rebuilt (kernel recompile + realloc).

        IMPORTANT: time_step and softening are per-launch kernel arguments,
        NOT reinit triggers — under adaptive dt the timestep changes every
        epoch and rebuilding the engine each time would recompile CUDA
        kernels per step.
        """
        return (
            self._engine is None
            or self._engine.n_bodies != n_bodies
            or self._engine.integration_method != integration_method
        )

    def _ensure_engine_arrays(
        self,
        positions: np.ndarray,
        velocities: np.ndarray,
        masses: np.ndarray,
        ids: np.ndarray,
        time_step: float,
        integration_method: str,
        softening_sq: float = SOFTENING_SQ,
    ) -> None:
        """Initialize the GPU engine, or upload new state to the existing one.

        Array-based: no per-body dict loops.

        Engine creation (rare: first job, or N/method change) goes through the
        dict constructor once. Every subsequent state upload is a vectorized
        structured-array fill.
        """
        # the engine knows the base integrators only. A g-named variant is its
        # base method on the whole system (the predictor matters for packages)
        integration_method = resolve_base_method(integration_method)
        if self._needs_engine_reinit(len(ids), integration_method):
            if self._engine is not None:
                # dropped first: if building the new engine fails (e.g. out of
                # GPU memory), the next job rebuilds instead of using freed buffers
                engine, self._engine = self._engine, None
                engine.cleanup()
            bodies = self._numpy_to_bodies(positions, velocities, masses, ids)
            self._engine = GPUNBodyEngine(
                bodies=bodies,
                time_step=time_step,
                integration_method=integration_method,
                gravitational_constant=G_CONST,
                softening_squared=softening_sq,
                use_float32=_gpu_use_float32(),
            )
        else:
            self._engine.update_bodies_from_arrays(positions, velocities, masses, ids)
            self._engine.set_time_step(time_step)
            self._engine.eps2 = softening_sq

    def _numpy_to_bodies(
        self,
        positions: np.ndarray,
        velocities: np.ndarray,
        masses: np.ndarray,
        ids: np.ndarray
    ) -> List[Dict]:
        """Convert numpy arrays to list of body dicts."""
        bodies = []
        for i in range(len(ids)):
            bodies.append({
                'idBody': int(ids[i]),
                'm': float(masses[i]),
                'x': float(positions[i, 0]),
                'y': float(positions[i, 1]),
                'z': float(positions[i, 2]),
                'vx': float(velocities[i, 0]),
                'vy': float(velocities[i, 1]),
                'vz': float(velocities[i, 2]),
            })
        return bodies

