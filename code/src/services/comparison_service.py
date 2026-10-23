# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Provides physics metric calculations for comparing N-body simulation runs.
All computations use Numba JIT for performance on medium-sized datasets (1K-10K bodies).

Key metrics:
- Energy conservation (kinetic + potential)
- Momentum conservation (linear)
- Angular momentum conservation
- Trajectory divergence (RMSE)
"""

import asyncio
import bisect
import contextlib
import json
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
from numba import jit

from async_file_service import EPOCH_RESULTS_BUCKET_NAME, AsyncS3Service
from constants import G_CONST, SOFTENING_SQ
from diagnostics import kinetic_energy, potential_energy_fast
from logging_config import setup_logging
from utils.snapshot_loader import (
    SnapshotReport,
    _get_snapshot_epochs,
    _parse_csv_to_numpy,
    read_body_frame,
    stream_job_snapshots,
)


logger = setup_logging('services.comparison')


# Two snapshot times closer than this (relative to max(1, |t|)) are the same time
TIME_TOLERANCE = 1e-9

# Comparison jobs processed at once (each streams its own and the reference
# job's snapshots, a few batches of them in memory at a time)
COMPARISON_CONCURRENCY = 4


def _job_info(job) -> dict:
    """The job's settings as shown with a comparison (JobComparisonSummary)."""
    return {
        "job_id": job.job_id,
        "status": job.status,
        "job_type": job.job_type,
        "num_epochs": job.num_epochs,
        "current_epoch": job.current_epoch,
        "snapshot_interval": job.snapshot_interval,
        "force_algorithm": job.force_algorithm or 'direct',
        "barnes_hut_theta": job.barnes_hut_theta,
        "softening_sq": job_softening_sq(job),
        "integration_method": job.integration_method or 'simple_euler',
        "time_step": job.time_step,
        "gpu_enabled": bool(job.gpu_enabled),
        "filename": job.filename,
        "description": job.description,
        "name": getattr(job, "name", None),
    }


def momentum_drift_percent(metrics_list) -> float:
    """|P_final - P_initial| as a percentage of sum_i m_i |v_i| at the start.
    Dividing by |P_initial| would divide by round-off for initial conditions
    in the centre-of-mass frame (P = 0 up to ~1e-13), turning noise into a
    large percentage. The sum of m|v| is the momentum scale of the system and is
    never ~0 for moving bodies."""
    if len(metrics_list) < 2:
        return 0.0
    scale = metrics_list[0].momentum_scale
    if not scale > 0:
        return 0.0
    return vector_drift(metrics_list[-1].momentum, metrics_list[0].momentum) / scale * 100


async def compute_full_comparison(ref_job, cmp_jobs, max_snapshots=None) -> dict:
    """The comparison of jobs (cmp_jobs) with a reference job (ref_job), all
    COMPLETED, as a dict of the JobToJobComparisonResponse schema. Used by both
    the ad-hoc POST /compare endpoint and the background comparison worker.

    Each comparison job is read once: its conservation metrics and its
    trajectory differences from the reference are computed from the same
    stream of snapshots, while the reference job's snapshots are streamed
    alongside in time order, so only a few snapshots of each job are in
    memory at a time. max_snapshots: of the reference job (every k-th back
    from its last), the comparison jobs are read at its times."""
    start_time = time.time()

    # Compute reference job energy metrics (using each job's own softening)
    ref_metrics = await compute_job_metrics_streaming(ref_job, max_snapshots=max_snapshots)
    ref_by_epoch = {m.epoch: m for m in ref_metrics.metrics}
    ref_times = [m.simulation_time for m in ref_metrics.metrics if m.epoch != 0]
    limit = asyncio.Semaphore(COMPARISON_CONCURRENCY)

    async def _compare_single(cmp_job):
        """Compare one job against reference."""
        problem = await bodies_differ(ref_job, cmp_job)
        if problem:
            return None, {"job_id": cmp_job.job_id, "reason": problem}
        # max_snapshots selects the reference's snapshots. The comparison job
        # is read at those times (each job subsampled on its own grid shared
        # only a few times, even when both grids were the same)
        cmp_epochs = (await comparison_epochs_at(cmp_job, ref_times)) if max_snapshots else None
        async with limit, contextlib.aclosing(timed_snapshots(ref_job, max_snapshots)) as ref_stream:
            pairing = TrajectoryPairing(ref_stream)
            cmp_metrics = await compute_job_metrics_streaming(
                cmp_job, on_snapshot=pairing.add, only_epochs=cmp_epochs)
        trajectory_comparisons = pairing.result()

        if not trajectory_comparisons:
            return None, {"job_id": cmp_job.job_id,
                          "reason": f"No overlapping time range between reference job {ref_job.job_id} and this job"}

        cmp_energy_sorted = sorted(
            [(m.simulation_time, m.total_energy) for m in cmp_metrics.metrics]
        )
        cmp_momentum_sorted = sorted(
            [(m.simulation_time, m.momentum_magnitude) for m in cmp_metrics.metrics]
        )

        kept = []
        energy_ref_ts, energy_cmp_ts = [], []
        momentum_ref_ts, momentum_cmp_ts = [], []

        for tc in trajectory_comparisons:
            ref_point = ref_by_epoch.get(tc.epoch)
            if ref_point is None:
                continue
            cmp_energy = interpolate_scalar_by_time(cmp_energy_sorted, tc.simulation_time)
            if cmp_energy is None:
                continue
            cmp_momentum = interpolate_scalar_by_time(cmp_momentum_sorted, tc.simulation_time) or 0.0
            kept.append(tc)
            energy_ref_ts.append(ref_point.total_energy)
            energy_cmp_ts.append(cmp_energy)
            momentum_ref_ts.append(ref_point.momentum_magnitude)
            momentum_cmp_ts.append(cmp_momentum)

        if not kept:
            return None, {"job_id": cmp_job.job_id,
                          "reason": "No time points with energy data available for both jobs"}

        result = {
            "job_id": cmp_job.job_id,
            "job_info": _job_info(cmp_job),
            "epochs_compared": len(kept),
            "position_rmse_final": kept[-1].position_rmse,
            "velocity_rmse_final": kept[-1].velocity_rmse,
            "energy_drift_reference": ref_metrics.energy_drift_percent,
            "energy_drift_comparison": cmp_metrics.energy_drift_percent,
            # the E0 the drifts are measured against (t = 0, or the first
            # snapshot when the input could not be read)
            "energy_initial_reference": ref_metrics.metrics[0].total_energy if ref_metrics.metrics else None,
            "energy_initial_comparison": cmp_metrics.metrics[0].total_energy if cmp_metrics.metrics else None,
            "momentum_drift_reference": momentum_drift_percent(ref_metrics.metrics),
            "momentum_drift_comparison": momentum_drift_percent(cmp_metrics.metrics),
            # the drifts are measured at each job's last snapshot
            "final_time_reference": float(ref_metrics.simulation_times[-1]) if ref_metrics.simulation_times else 0.0,
            "final_time_comparison": float(cmp_metrics.simulation_times[-1]) if cmp_metrics.simulation_times else 0.0,
            "interpolated_points": sum(1 for tc in kept if tc.interpolated),
            "simulation_times_exact": ref_metrics.simulation_times_exact and cmp_metrics.simulation_times_exact,
            "snapshots_missing_reference": ref_metrics.snapshots_missing,
            "snapshots_missing_comparison": cmp_metrics.snapshots_missing,
            "epochs": [tc.epoch for tc in kept],
            "simulation_times": [float(tc.simulation_time) for tc in kept],
            "position_rmse_timeseries": [float(tc.position_rmse) for tc in kept],
            "velocity_rmse_timeseries": [float(tc.velocity_rmse) for tc in kept],
            "energy_reference_timeseries": [float(v) for v in energy_ref_ts],
            "energy_comparison_timeseries": [float(v) for v in energy_cmp_ts],
            "momentum_reference_timeseries": [float(v) for v in momentum_ref_ts],
            "momentum_comparison_timeseries": [float(v) for v in momentum_cmp_ts],
        }
        return result, None

    results = await asyncio.gather(*[_compare_single(job) for job in cmp_jobs])

    comparisons = []
    skipped_jobs = []
    for result, skipped in results:
        if result:
            comparisons.append(result)
        if skipped:
            skipped_jobs.append(skipped)

    computation_time_ms = int((time.time() - start_time) * 1000)

    return {
        "reference_job_id": ref_job.job_id,
        "reference_job_info": _job_info(ref_job),
        "comparisons": comparisons,
        "skipped_jobs": skipped_jobs,
        "computation_time_ms": computation_time_ms,
    }


# Data Classes for Results

@dataclass
class EpochMetrics:
    """Conservation metrics at a single epoch."""
    epoch: int
    simulation_time: float
    kinetic_energy: float
    potential_energy: float
    total_energy: float
    relative_energy_error: float  # (E - E0) / |E0|
    momentum: Tuple[float, float, float]
    momentum_magnitude: float
    angular_momentum: Tuple[float, float, float]
    angular_momentum_magnitude: float
    # sum_i m_i |v_i|: the scale a momentum change is measured against
    momentum_scale: float = 0.0


@dataclass
class TrajectoryComparison:
    """Trajectory divergence at one of the reference job's snapshots."""
    epoch: int              # the reference job's epoch
    simulation_time: float  # the reference job's simulation time
    position_rmse: float
    velocity_rmse: float
    position_max_error: float
    velocity_max_error: float
    # the comparison job had no snapshot at this time. Its state was
    # interpolated between the two around it
    interpolated: bool = False


@dataclass
class JobMetricsResult:
    """Complete metrics result for a job."""
    job_id: int
    epochs: List[int]
    simulation_times: List[float]
    metrics: List[EpochMetrics]
    energy_drift_percent: float
    momentum_conserved: bool  # see conservation_flags
    angular_momentum_conserved: bool
    computation_time_ms: int
    # False when an adaptive job's simulation times were not all recorded and
    # estimates from the recorded ones stand in for some of them (snapshot_time)
    simulation_times_exact: bool = True
    # snapshots the job should have but that do not exist (left out)
    snapshots_missing: int = 0


# Numba JIT Functions - Core Physics Calculations

@jit(nopython=True, nogil=True, cache=True)
def compute_momentum(velocities: np.ndarray, masses: np.ndarray) -> Tuple[float, float, float]:
    """Total linear momentum (Px, Py, Pz) = sum(m * v)."""
    n = len(masses)
    px, py, pz = 0.0, 0.0, 0.0

    for i in range(n):
        px += masses[i] * velocities[i, 0]
        py += masses[i] * velocities[i, 1]
        pz += masses[i] * velocities[i, 2]

    return (px, py, pz)


@jit(nopython=True, nogil=True, cache=True)
def compute_angular_momentum(
    positions: np.ndarray,
    velocities: np.ndarray,
    masses: np.ndarray
) -> Tuple[float, float, float]:
    """Total angular momentum (Lx, Ly, Lz) = sum(m * r x v)."""
    n = len(masses)
    lx, ly, lz = 0.0, 0.0, 0.0

    for i in range(n):
        m = masses[i]
        x, y, z = positions[i, 0], positions[i, 1], positions[i, 2]
        vx, vy, vz = velocities[i, 0], velocities[i, 1], velocities[i, 2]

        # Cross product: r x v
        lx += m * (y * vz - z * vy)
        ly += m * (z * vx - x * vz)
        lz += m * (x * vy - y * vx)

    return (lx, ly, lz)


@jit(nopython=True, nogil=True, cache=True)
def compute_trajectory_rmse(
    pos_a: np.ndarray,
    pos_b: np.ndarray,
    vel_a: np.ndarray,
    vel_b: np.ndarray
) -> Tuple[float, float, float, float]:
    """(position_rmse, velocity_rmse, position_max_error, velocity_max_error)
    between two runs' states, their bodies matched by index."""
    n = len(pos_a)

    # Handle empty arrays
    if n == 0:
        return (0.0, 0.0, 0.0, 0.0)

    pos_sum_sq = 0.0
    vel_sum_sq = 0.0
    pos_max = 0.0
    vel_max = 0.0

    for i in range(n):
        # Position error
        dx = pos_a[i, 0] - pos_b[i, 0]
        dy = pos_a[i, 1] - pos_b[i, 1]
        dz = pos_a[i, 2] - pos_b[i, 2]
        pos_err_sq = dx*dx + dy*dy + dz*dz
        pos_sum_sq += pos_err_sq
        pos_err = np.sqrt(pos_err_sq)
        if pos_err > pos_max:
            pos_max = pos_err

        # Velocity error
        dvx = vel_a[i, 0] - vel_b[i, 0]
        dvy = vel_a[i, 1] - vel_b[i, 1]
        dvz = vel_a[i, 2] - vel_b[i, 2]
        vel_err_sq = dvx*dvx + dvy*dvy + dvz*dvz
        vel_sum_sq += vel_err_sq
        vel_err = np.sqrt(vel_err_sq)
        if vel_err > vel_max:
            vel_max = vel_err

    pos_rmse = np.sqrt(pos_sum_sq / n)
    vel_rmse = np.sqrt(vel_sum_sq / n)

    return (pos_rmse, vel_rmse, pos_max, vel_max)


# Interpolation Utilities for Time-Based Comparison

# Type alias for snapshot data: (positions, velocities, masses)
SnapshotData = Tuple[np.ndarray, np.ndarray, np.ndarray]


def hermite_interpolate(t0: float, state0: SnapshotData, t1: float, state1: SnapshotData,
                        t: float) -> SnapshotData:
    """State at t between snapshots at t0 and t1, by cubic Hermite
    interpolation of the positions with the snapshots' velocities as their
    derivatives. The velocities are the interpolant's derivative. Errors are
    O(h^4) in position and O(h^3) in velocity (h = t1 - t0), where linear
    interpolation, O(h^2), showed a circular orbit sampled every 0.3 time
    units as 1% of its radius away from itself."""
    pos0, vel0, masses = state0
    pos1, vel1, _ = state1
    h = t1 - t0
    u = (t - t0) / h
    u2, u3 = u * u, u * u * u
    pos = ((2 * u3 - 3 * u2 + 1) * pos0 + (u3 - 2 * u2 + u) * h * vel0
           + (3 * u2 - 2 * u3) * pos1 + (u3 - u2) * h * vel1)
    vel = ((6 * u2 - 6 * u) / h * (pos0 - pos1)
           + (3 * u2 - 4 * u + 1) * vel0 + (3 * u2 - 2 * u) * vel1)
    return pos, vel, masses


def _trajectory_point(epoch: int, t: float, ref_state: SnapshotData, cmp_state: SnapshotData,
                      interpolated: bool) -> Optional[TrajectoryComparison]:
    """Differences of two states of the same bodies (None if the body counts differ)."""
    if len(ref_state[0]) != len(cmp_state[0]):
        logger.warning(f"Body count mismatch at time {t}: {len(ref_state[0])} vs {len(cmp_state[0])}")
        return None
    pos_rmse, vel_rmse, pos_max, vel_max = compute_trajectory_rmse(
        ref_state[0], cmp_state[0], ref_state[1], cmp_state[1])
    return TrajectoryComparison(
        epoch=epoch, simulation_time=t,
        position_rmse=pos_rmse, velocity_rmse=vel_rmse,
        position_max_error=pos_max, velocity_max_error=vel_max,
        interpolated=interpolated,
    )


class TrajectoryPairing:
    """Pairs the reference job's snapshots with a comparison job's, both
    arriving in time order, in one pass over each.

    The comparison job's snapshots are fed with add(). The reference job's
    are pulled from `ref_stream` (an async iterator of (epoch, time, state))
    up to each one. A reference time that the comparison job also has is
    compared directly. One between two comparison snapshots is compared with
    the interpolated state. Reference times outside the comparison job's
    range are left out.
    """

    def __init__(self, ref_stream):
        self._ref = ref_stream
        self._pending = None    # the next reference snapshot, not yet reached
        self._ref_done = False
        self._prev = None       # the previous comparison snapshot: (time, state)
        self._last_ref_t = float('-inf')
        self.points: List[TrajectoryComparison] = []

    async def add(self, epoch: int, t: float, state: SnapshotData) -> None:
        if self._prev is not None and t <= self._prev[0]:
            # out of time order (an estimated adaptive time): pairing or
            # interpolating against it would compare states at wrong times
            logger.warning(f"Comparison snapshot {epoch} at t={t} is not after the previous one "
                           f"(t={self._prev[0]}), left out of the trajectory comparison")
            return
        while not self._ref_done:
            if self._pending is None:
                try:
                    self._pending = await self._ref.__anext__()
                except StopAsyncIteration:
                    self._ref_done = True
                    break
            ref_epoch, ref_t, ref_state = self._pending
            if ref_t < self._last_ref_t:
                logger.warning(f"Reference snapshot {ref_epoch} at t={ref_t} is out of time order, "
                               f"left out of the trajectory comparison")
                self._pending = None
                continue
            self._last_ref_t = ref_t
            tol = TIME_TOLERANCE * max(1.0, abs(ref_t))
            if ref_t > t + tol:
                break
            self._pending = None
            if abs(ref_t - t) <= tol:
                cmp_state, interpolated = state, False
            elif self._prev is not None:
                cmp_state, interpolated = hermite_interpolate(*self._prev, t, state, ref_t), True
            else:
                continue        # before the comparison job's first snapshot
            point = await asyncio.to_thread(_trajectory_point, ref_epoch, ref_t, ref_state,
                                            cmp_state, interpolated)
            if point is not None:
                self.points.append(point)
        self._prev = (t, state)

    def result(self) -> List[TrajectoryComparison]:
        """The compared points. When the times both jobs have form a regular
        sub-grid of the reference's points over the whole overlap (two jobs
        with steps dt and k*dt and the same snapshot interval share every
        k-th reference time), only those: they need no interpolation.
        Otherwise all points, the interpolated ones marked as such. Keeping
        only a few coincidental shared times (adaptive steps, differently
        subsampled grids) threw most of the series away."""
        exact = [i for i, p in enumerate(self.points) if not p.interpolated]
        if len(exact) < 2 or exact[-1] != len(self.points) - 1:
            return self.points
        stride = exact[1] - exact[0]
        regular = all(b - a == stride for a, b in zip(exact, exact[1:])) and exact[0] < stride
        return [self.points[i] for i in exact] if regular else self.points


def interpolate_scalar_by_time(
    time_values: List[Tuple[float, float]],
    target_time: float,
    tolerance: float = 1e-9
) -> Optional[float]:
    """The value at target_time, linearly interpolated from sorted (time, value)
    pairs (an exact match within the relative tolerance is returned as it is).
    None outside their range."""
    if not time_values:
        return None

    times = [tv[0] for tv in time_values]

    # Check bounds
    if target_time < times[0] - tolerance or target_time > times[-1] + tolerance:
        return None

    idx = bisect.bisect_right(times, target_time)

    # Check exact match with left neighbor
    if idx > 0 and abs(times[idx - 1] - target_time) <= tolerance * max(1.0, abs(target_time)):
        return time_values[idx - 1][1]
    # Check exact match with right neighbor
    if idx < len(times) and abs(times[idx] - target_time) <= tolerance * max(1.0, abs(target_time)):
        return time_values[idx][1]

    # Interpolate
    if idx == 0 or idx >= len(times):
        return None

    t_before, v_before = time_values[idx - 1]
    t_after, v_after = time_values[idx]
    alpha = (target_time - t_before) / (t_after - t_before)
    return v_before + alpha * (v_after - v_before)


# High-Level Metric Computation Functions

def compute_all_metrics(
    positions: np.ndarray,
    velocities: np.ndarray,
    masses: np.ndarray,
    epoch: int,
    initial_energy: Optional[float] = None,
    simulation_time: float = 0.0,
    softening_sq: float = SOFTENING_SQ,
) -> EpochMetrics:
    """The conservation metrics (EpochMetrics) of one snapshot, the energy error
    relative to initial_energy."""
    # Energy calculations
    ke = kinetic_energy(velocities, masses)
    pe = potential_energy_fast(positions, masses, softening_sq, G_CONST)
    total_energy = ke + pe

    # Relative energy error
    if initial_energy is not None and abs(initial_energy) > 1e-15:
        relative_error = (total_energy - initial_energy) / abs(initial_energy)
    else:
        relative_error = 0.0

    # Momentum calculations
    px, py, pz = compute_momentum(velocities, masses)
    p_magnitude = np.sqrt(px*px + py*py + pz*pz)
    momentum_scale = float(np.sum(np.abs(masses) * np.sqrt(np.sum(velocities * velocities, axis=1))))

    # Angular momentum calculations
    lx, ly, lz = compute_angular_momentum(positions, velocities, masses)
    l_magnitude = np.sqrt(lx*lx + ly*ly + lz*lz)

    return EpochMetrics(
        epoch=epoch,
        simulation_time=simulation_time,
        kinetic_energy=ke,
        potential_energy=pe,
        total_energy=total_energy,
        relative_energy_error=relative_error,
        momentum=(px, py, pz),
        momentum_magnitude=p_magnitude,
        angular_momentum=(lx, ly, lz),
        angular_momentum_magnitude=l_magnitude,
        momentum_scale=momentum_scale,
    )


def vector_drift(v, v0) -> float:
    """|v - v0|, the change of a conserved vector. Comparing magnitudes
    (| |v| - |v0| |) would report a rotated momentum as conserved."""
    return float(np.linalg.norm(np.asarray(v, dtype=np.float64) - np.asarray(v0, dtype=np.float64)))


def conservation_flags(metrics_list: List[EpochMetrics], tol: float = 0.01, floor: float = 1e-10) -> Tuple[bool, bool]:
    """Whether linear and angular momentum stay within `tol` of the first
    entry (the initial state when it was loaded), as vectors. Linear
    momentum is measured against sum m|v| (momentum_drift_percent). Angular
    momentum against its initial magnitude, or absolutely (`floor`) when
    that is ~0."""
    def conserved(vectors, scale):
        v0 = vectors[0]
        limit = tol * scale if scale > floor else floor
        return all(vector_drift(v, v0) <= limit for v in vectors)
    if not metrics_list:
        return True, True
    first = metrics_list[0]
    p_scale = first.momentum_scale or float(np.linalg.norm(first.momentum))
    return (conserved([m.momentum for m in metrics_list], p_scale),
            conserved([m.angular_momentum for m in metrics_list], float(np.linalg.norm(first.angular_momentum))))


# Streaming Metric Computation (Memory-Efficient)

async def _load_adaptive_sim_times(job, fetch_batch_size: int = 50) -> Dict[int, float]:
    """Fetch all adaptive-dt metadata sidecars for a job in parallel batches.

    Returns {epoch: simulation_time} for every sidecar found. Epochs without
    a sidecar are simply absent (callers fall back to epoch * time_step).
    """


    snapshot_epochs = _get_snapshot_epochs(job.num_epochs, job.snapshot_interval)
    if not snapshot_epochs:
        return {}

    service = AsyncS3Service()
    sim_times: Dict[int, float] = {}
    # the monolithic worker writes metadata.json next to each epoch CSV. The
    # distributed one records the time in each snapshot's aux sidecar
    if job.job_type == 'distributed_v2':
        def key_of(e):
            return f'v2/job_id/{job.job_id}/aux_{e}.json'
    else:
        def key_of(e):
            return f'job_id/{job.job_id}/epoch_num/{e}/metadata.json'

    # Batched so a 2000-snapshot job doesn't open 2000 concurrent connections
    for batch_start in range(0, len(snapshot_epochs), fetch_batch_size):
        batch = snapshot_epochs[batch_start:batch_start + fetch_batch_size]
        keys = [key_of(e) for e in batch]
        # a missing sidecar is skipped (its time is estimated). Any other
        # error is raised rather than silently giving the snapshot a wrong time
        results = await service.download_files_batch(EPOCH_RESULTS_BUCKET_NAME, keys, raise_errors=True)
        for epoch, (_key, content) in zip(batch, results):
            if content is None:
                continue
            try:
                meta = json.loads(content)
                if meta.get('simulation_time') is not None:
                    sim_times[epoch] = float(meta['simulation_time'])
            except (ValueError, TypeError):
                continue

    logger.info(f"Loaded {len(sim_times)}/{len(snapshot_epochs)} adaptive metadata sidecars for job {job.job_id}")
    return sim_times


async def input_body_ids(job) -> Optional[np.ndarray]:
    """Sorted idBody of the job's input file (None if it has no such column)."""
    raw = await AsyncS3Service().download_file(job.input_bucket, job.input_file_key)
    df = await asyncio.to_thread(read_body_frame, raw)
    if "idBody" not in df.columns:
        return None
    return np.sort(df["idBody"].to_numpy().astype(np.int64))


async def bodies_differ(ref_job, cmp_job) -> Optional[str]:
    """Why the two jobs' trajectories cannot be compared body by body (None
    if they can). The snapshots are put in idBody order and then differenced
    row by row, so two inputs with the same N but different bodies would give
    a plausible-looking error."""
    if (ref_job.input_bucket, ref_job.input_file_key) == (cmp_job.input_bucket, cmp_job.input_file_key):
        return None
    try:
        a, b = await asyncio.gather(input_body_ids(ref_job), input_body_ids(cmp_job))
    except Exception as e:  # noqa: BLE001
        return f"could not read the input files to check that the jobs simulate the same bodies: {e}"
    if a is None or b is None:
        return "an input file has no idBody column, so bodies cannot be matched across the jobs"
    if a.shape != b.shape or not np.array_equal(a, b):
        return (f"the jobs simulate different bodies ({len(a)} vs {len(b)}, "
                f"{len(np.setxor1d(a, b))} IDs not in both inputs)")
    return None


async def load_initial_state(job):
    """(positions, velocities, masses) of the job's input file, or None (with a
    warning) if it cannot be read. The metrics then fall back to the first
    snapshot as their baseline."""
    try:
        raw = await AsyncS3Service().download_file(job.input_bucket, job.input_file_key)
        return await asyncio.to_thread(_parse_csv_to_numpy, raw)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Job {job.job_id}: initial state unavailable ({e}), "
                       f"conservation is measured from the first snapshot")
        return None


def job_softening_sq(job) -> float:
    """The softening the job was integrated with (workers fall back to
    SOFTENING_SQ when the row has none)."""
    eps2 = job.softening_sq
    return SOFTENING_SQ if eps2 is None else float(eps2)


async def _job_sim_times(job) -> Dict[int, float]:
    """Recorded simulation times of an adaptive job's snapshots ({} for a
    fixed-step job, whose times are epoch * time_step)."""
    if job.adaptive_enabled:
        return await _load_adaptive_sim_times(job)
    return {}


def snapshot_time(epoch: int, recorded: Dict[int, float], time_step: float) -> float:
    """Simulation time of a snapshot: the recorded one, or for an adaptive
    snapshot whose record is missing an estimate between the nearest
    recorded ones (linear in the epoch, t = 0 at epoch 0, past the last
    record, at the last interval's rate). epoch * time_step instead put such
    a snapshot out of time order (adaptive steps shrink) and the comparison
    interpolated far outside its interval: errors of 1e2 on a unit orbit."""
    if epoch in recorded:
        return recorded[epoch]
    if not recorded:
        return epoch * time_step
    known = sorted(recorded)
    i = bisect.bisect_left(known, epoch)
    e0, t0 = (known[i - 1], recorded[known[i - 1]]) if i > 0 else (0, 0.0)
    if i < len(known):
        e1, t1 = known[i], recorded[known[i]]
    else:       # past the last record: continue at the rate of the interval before it
        e1, t1 = e0, t0
        e0, t0 = (known[i - 2], recorded[known[i - 2]]) if i > 1 else (0, 0.0)
        if e1 == e0:
            return epoch * time_step
    return t0 + (epoch - e0) * (t1 - t0) / (e1 - e0)


async def comparison_epochs_at(job, times: List[float]) -> List[int]:
    """The job's planned snapshot epochs needed to compare it at `times`: the
    one at each time, or the two around it, and its last snapshot (where its
    drift is measured)."""
    planned = _get_snapshot_epochs(job.num_epochs, job.snapshot_interval)
    if not planned:
        return []
    recorded = await _job_sim_times(job)
    planned_times = [snapshot_time(e, recorded, job.time_step) for e in planned]
    needed = {planned[-1]}
    for t in times:
        tol = TIME_TOLERANCE * max(1.0, abs(t))
        i = bisect.bisect_left(planned_times, t - tol)
        if i < len(planned) and abs(planned_times[i] - t) <= tol:
            needed.add(planned[i])
        elif 0 < i < len(planned):
            needed.update((planned[i - 1], planned[i]))
    return sorted(needed)


async def timed_snapshots(job, max_snapshots: Optional[int] = None, batch_size: int = 10,
                          report=None, sim_times: Optional[Dict[int, float]] = None, epochs=None):
    """The job's snapshots in epoch order as (epoch, simulation time, state).
    The time is the recorded one for an adaptive job (estimated where the
    record is missing, see snapshot_time), else epoch * time_step."""
    if sim_times is None:
        sim_times = await _job_sim_times(job)
    stream = stream_job_snapshots(job, batch_size=batch_size, max_snapshots=max_snapshots, report=report,
                                  epochs=epochs)
    async with contextlib.aclosing(stream):
        async for epoch, state in stream:
            yield epoch, snapshot_time(epoch, sim_times, job.time_step), state


async def compute_job_metrics_streaming(
    job,
    max_snapshots: Optional[int] = None,
    batch_size: int = 10,
    softening_sq: Optional[float] = None,
    on_snapshot=None,
    only_epochs=None,
) -> JobMetricsResult:
    """The job's metrics (JobMetricsResult), streaming its snapshots: one at a
    time is held, besides the metrics.

    max_snapshots: every k-th back from the last
    (utils.snapshot_loader.select_snapshot_epochs), or only_epochs: exactly
    these. batch_size snapshots are fetched in parallel. softening_sq
    overrides the job's own (by default the potential energy is the one the
    integrator used). on_snapshot: a coroutine function called with (epoch,
    time, state) for every snapshot, so a caller can use the same stream."""

    start_time = time.time()
    if softening_sq is None:
        softening_sq = job_softening_sq(job)

    epochs = []
    metrics_list = []
    initial_energy = None

    # All simulation-time sidecars of an adaptive job, fetched in parallel
    # batches before the snapshots
    sim_times_cache = await _job_sim_times(job)
    is_adaptive = job.adaptive_enabled

    # The baseline is the initial state (t = 0), not the first snapshot that
    # happened to load: snapshots start at the first interval, and one that
    # fails to load would silently move the baseline.
    initial = await load_initial_state(job)
    if initial is not None:
        metrics = await asyncio.to_thread(compute_all_metrics, *initial, 0, None,
                                          simulation_time=0.0, softening_sq=softening_sq)
        initial_energy = metrics.total_energy
        epochs.append(0)
        metrics_list.append(metrics)

    report = SnapshotReport()
    async for epoch, epoch_sim_time, (positions, velocities, masses) in timed_snapshots(
        job, max_snapshots, batch_size, report=report, sim_times=sim_times_cache, epochs=only_epochs,
    ):
        if epoch == 0 and initial is not None:
            continue

        # Off-loop: O(N^2) potential energy per snapshot. With CSV parsing and
        # this both on worker threads, the event loop (and the /api/health
        # liveness probe) stays responsive throughout the computation.
        metrics = await asyncio.to_thread(
            compute_all_metrics,
            positions, velocities, masses, epoch, initial_energy,
            simulation_time=epoch_sim_time,
            softening_sq=softening_sq,
        )

        if initial_energy is None:
            initial_energy = metrics.total_energy

        epochs.append(epoch)
        metrics_list.append(metrics)
        if on_snapshot is not None:
            await on_snapshot(epoch, epoch_sim_time, (positions, velocities, masses))

        # Log progress for long-running computations
        if len(metrics_list) % 50 == 0:
            logger.info(f"Job {job.job_id}: processed {len(metrics_list)} snapshots...")

    if len(metrics_list) >= 2 and initial_energy is not None and abs(initial_energy) > 1e-15:
        final_energy = metrics_list[-1].total_energy
        energy_drift_percent = abs((final_energy - initial_energy) / initial_energy) * 100
    else:
        energy_drift_percent = 0.0

    momentum_conserved, angular_momentum_conserved = conservation_flags(metrics_list)

    computation_time_ms = int((time.time() - start_time) * 1000)

    logger.info(
        f"Computed metrics (streaming) for job {job.job_id}: {len(epochs)} epochs, "
        f"energy drift {energy_drift_percent:.4f}%, took {computation_time_ms}ms"
    )
    # An adaptive job's time axis is exact only where its recorded clock was
    # found. Elsewhere an estimate stands in (snapshot_time), and the result says so
    times_exact = not is_adaptive or all(e == 0 or e in sim_times_cache for e in epochs)
    if not times_exact:
        missing = sum(1 for e in epochs if e != 0 and e not in sim_times_cache)
        logger.warning(f"Job {job.job_id} is adaptive but {missing} of its {len(epochs)} snapshots have no "
                       f"recorded simulation time, their times are estimated from the recorded ones")

    return JobMetricsResult(
        job_id=job.job_id,
        epochs=epochs,
        simulation_times=[m.simulation_time for m in metrics_list],
        metrics=metrics_list,
        energy_drift_percent=energy_drift_percent,
        momentum_conserved=momentum_conserved,
        angular_momentum_conserved=angular_momentum_conserved,
        computation_time_ms=computation_time_ms,
        simulation_times_exact=times_exact,
        snapshots_missing=len(report.missing),
    )


# JIT Warmup Function

def warmup_comparison_jit() -> None:
    """
    Pre-compile JIT functions at startup to avoid first-request latency.

    Call this from api.py startup event.
    """
    # Arrays as production gets them (the snapshot parser's layout): another
    # layout compiles a separate signature
    rows = "".join(f"{i},{i + 1},{1.0 + i},{i},{-i},{2 * i},{0.1 * i},{0.2},{-0.3 * i}\n" for i in range(10))
    positions, velocities, masses = _parse_csv_to_numpy("n,idBody,m,x,y,z,vx,vy,vz\n" + rows)

    _ = kinetic_energy(velocities, masses)
    # softening_sq passed as compute_all_metrics passes it: an omitted
    # argument compiles a separate signature
    _ = potential_energy_fast(positions, masses, float(SOFTENING_SQ), float(G_CONST))
    _ = compute_momentum(velocities, masses)
    _ = compute_angular_momentum(positions, velocities, masses)
    _ = compute_trajectory_rmse(positions, positions, velocities, velocities)

    logger.info("Comparison JIT functions warmed up")
