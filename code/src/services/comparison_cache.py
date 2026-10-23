# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Cache for comparison results.

Two layers:
- L1: in-memory with TTL (fast, lost on pod restart)
- L2: S3-persisted JSON for COMPLETED jobs only (survives restarts, a
  completed job's snapshots are immutable, so its metrics never change
  and the persistent entry never expires)

Comparison results are expensive to compute (O(N^2) potential energy
across every snapshot): recomputing them after each pod restart would keep
large jobs in liveness-probe kill loops.
"""

import json
import time
from threading import Lock
from typing import Any, Dict, Optional, Tuple

from async_file_service import EPOCH_RESULTS_BUCKET_NAME, AsyncS3Service
from file_service import delete_files_by_prefix
from logging_config import setup_logging


logger = setup_logging('services.comparison_cache')

# Version the persistent key so a format or semantics change invalidates old
# entries instead of failing to parse them. A change of comparison semantics
# usually also bumps
# routes.comparison.COMPARISON_ALGORITHM_VERSION (saved comparisons).
_PERSISTENT_CACHE_VERSION = 'v5'

# Cache TTL settings
COMPARISON_CACHE_TTL = 600  # 10 minutes - longer than animation since data changes less

# Cache for single job metrics: (job_id, max_snapshots) -> (JobMetricsResult as dict, timestamp)
# Key includes max_snapshots because results differ based on this parameter
_metrics_cache: Dict[Tuple[int, int], Tuple[Dict[str, Any], float]] = {}
_metrics_lock = Lock()


def get_cached_metrics(job_id: int, max_snapshots: int) -> Optional[Dict[str, Any]]:
    """The job's cached metrics for max_snapshots (part of the key), as a dict.
    None if not cached or expired."""
    cache_key = (job_id, max_snapshots)
    with _metrics_lock:
        if cache_key in _metrics_cache:
            data, timestamp = _metrics_cache[cache_key]
            if time.time() - timestamp < COMPARISON_CACHE_TTL:
                logger.debug(f"Cache hit for job {job_id} metrics (max_snapshots={max_snapshots})")
                return data
            del _metrics_cache[cache_key]
            logger.debug(f"Cache expired for job {job_id} metrics")

    return None


def cache_metrics(job_id: int, max_snapshots: int, metrics_result: Any) -> None:
    """Cache the job's metrics (a JobMetricsResult) for max_snapshots (part of
    the key)."""
    result_dict = _metrics_result_to_dict(metrics_result)
    cache_key = (job_id, max_snapshots)

    with _metrics_lock:
        _metrics_cache[cache_key] = (result_dict, time.time())
        logger.debug(f"Cached metrics for job {job_id} (max_snapshots={max_snapshots})")


def _metrics_result_to_dict(result: Any) -> Dict[str, Any]:
    """Convert JobMetricsResult to serializable dict."""
    return {
        'job_id': result.job_id,
        'epochs': result.epochs,
        'simulation_times': result.simulation_times,
        'metrics': [
            {
                'epoch': m.epoch,
                'simulation_time': m.simulation_time,
                'kinetic_energy': m.kinetic_energy,
                'potential_energy': m.potential_energy,
                'total_energy': m.total_energy,
                'relative_energy_error': m.relative_energy_error,
                'momentum': list(m.momentum),
                'momentum_magnitude': m.momentum_magnitude,
                'angular_momentum': list(m.angular_momentum),
                'angular_momentum_magnitude': m.angular_momentum_magnitude
            }
            for m in result.metrics
        ],
        'energy_drift_percent': result.energy_drift_percent,
        'momentum_conserved': result.momentum_conserved,
        'angular_momentum_conserved': result.angular_momentum_conserved,
        'computation_time_ms': result.computation_time_ms,
        'simulation_times_exact': getattr(result, 'simulation_times_exact', True),
        'snapshots_missing': getattr(result, 'snapshots_missing', 0),
    }


# L2: S3-persisted metrics for completed (immutable) jobs

def _persistent_metrics_key(job_id: int, max_snapshots: Optional[int]) -> str:
    suffix = 'all' if max_snapshots is None else str(max_snapshots)
    return f'job_id/{job_id}/metrics_cache/{_PERSISTENT_CACHE_VERSION}_max_{suffix}.json'


async def get_persistent_metrics(job_id: int, max_snapshots: Optional[int]) -> Optional[Dict[str, Any]]:
    """Fetch persisted metrics for a completed job from S3.

    Returns the JobMetricsResult dict, or None if not stored / unreadable.
    On a hit, also populates the in-memory L1 cache.
    """

    key = _persistent_metrics_key(job_id, max_snapshots)
    try:
        raw = await AsyncS3Service().download_file(EPOCH_RESULTS_BUCKET_NAME, key)
        data = json.loads(raw)
    except Exception:
        return None

    with _metrics_lock:
        _metrics_cache[(job_id, max_snapshots)] = (data, time.time())
    logger.info(f"Persistent cache hit for job {job_id} metrics (max_snapshots={max_snapshots})")
    return data


async def store_persistent_metrics(
    job_id: int,
    max_snapshots: Optional[int],
    metrics_result: Any,
) -> None:
    """Persist computed metrics for a completed job to S3 (best-effort).

    Only call for COMPLETED jobs — their snapshots are immutable, so the
    entry never needs invalidation. A failure here is logged and swallowed:
    the caller already has the computed result.
    """

    key = _persistent_metrics_key(job_id, max_snapshots)
    try:
        payload = json.dumps(_metrics_result_to_dict(metrics_result))
        await AsyncS3Service().upload_file(EPOCH_RESULTS_BUCKET_NAME, key, payload)
        logger.info(f"Persisted metrics for job {job_id} (max_snapshots={max_snapshots})")
    except Exception as e:
        logger.warning(f"Failed to persist metrics for job {job_id}: {e}")


def delete_persistent_metrics(job_id: int) -> int:
    """Delete every persisted metrics entry of a job (all versions and
    snapshot limits). Invalidating only the in-memory cache would let the
    next lookup reload the old result from S3. Returns the
    number of objects deleted."""
    return delete_files_by_prefix(EPOCH_RESULTS_BUCKET_NAME, f'job_id/{job_id}/metrics_cache/')


def invalidate_job_cache(job_id: int) -> int:
    """Drop every cache entry of the job (its data changed). Returns how many."""
    count = 0

    with _metrics_lock:
        # Find all cache entries for this job (any max_snapshots value)
        keys_to_delete = [
            k for k in _metrics_cache.keys()
            if k[0] == job_id  # k is (job_id, max_snapshots)
        ]
        for key in keys_to_delete:
            del _metrics_cache[key]
        count += len(keys_to_delete)

    if count > 0:
        logger.info(f"Invalidated {count} cache entries for job {job_id}")

    return count


def clear_expired_comparison_cache() -> int:
    """Remove every expired entry, periodically (e.g. from the main cache
    cleanup task). Returns how many."""
    current_time = time.time()
    deleted_count = 0

    with _metrics_lock:
        keys_to_delete = [
            cache_key for cache_key, (_, timestamp) in _metrics_cache.items()
            if current_time - timestamp >= COMPARISON_CACHE_TTL
        ]
        for cache_key in keys_to_delete:
            del _metrics_cache[cache_key]
        deleted_count += len(keys_to_delete)

    return deleted_count


def get_cache_stats() -> Dict[str, Any]:
    """The cache's statistics, for monitoring."""
    with _metrics_lock:
        metrics_count = len(_metrics_cache)

    return {
        'metrics_cache_size': metrics_count,
        'metrics_ttl_seconds': COMPARISON_CACHE_TTL,
    }
