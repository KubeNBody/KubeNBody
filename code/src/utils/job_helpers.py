# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Functions for job progress calculation, validation, and status management.
"""

from typing import Optional

from fastapi import HTTPException
from starlette.status import HTTP_400_BAD_REQUEST

from constants import DEFAULT_THETA
from integrators import IntegrationMethod
from logging_config import setup_logging
from models import Job, Package
from schemas import JobStatus, PackageStatus
from v2.partitioning import VALID_PARTITION_STRATEGIES


logger = setup_logging('utils.job_helpers')

VALID_FORCE_ALGORITHMS = ("direct", "barnes_hut")
# Single source of truth: the IntegrationMethod enum. This includes the plain
# integrators AND the distributed predicted-background variants (grk4_accel,
# gfr_accel, gvv_linear, …), so those are accepted by the API automatically.
VALID_INTEGRATION_METHODS = tuple(m.value for m in IntegrationMethod)


def snapshot_epochs(last_epoch: int, snapshot_interval: int, include_last: bool = True) -> list:
    """Epochs with a snapshot up to last_epoch: every snapshot_interval-th,
    plus last_epoch itself (include_last, the final epoch is always
    snapshotted). An interval of 0 or less leaves only last_epoch (a modulo
    by it raised ZeroDivisionError). Built arithmetically: num_epochs can be
    very large."""
    if last_epoch <= 0:
        return []
    if snapshot_interval <= 0:
        return [last_epoch] if include_last else []
    epochs = list(range(snapshot_interval, last_epoch + 1, snapshot_interval))
    if include_last and last_epoch % snapshot_interval:
        epochs.append(last_epoch)
    return epochs


def validate_force_algorithm(force_algorithm: str, use_gpu: bool = False) -> None:
    """400 for an unknown force algorithm or one the GPU does not support."""
    if force_algorithm not in VALID_FORCE_ALGORITHMS:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Invalid force_algorithm '{force_algorithm}'. Must be 'direct' or 'barnes_hut'"
        )
    if force_algorithm == "barnes_hut" and use_gpu:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="Barnes-Hut algorithm is not supported with GPU acceleration. Please use 'direct' "
                   "algorithm with GPU, or disable GPU for Barnes-Hut."
        )


def validate_integration_method(integration_method: str, use_gpu: bool = False) -> None:
    """400 for an unknown integration method. Every method runs on both CPU and
    GPU (use_gpu is kept for signature stability)."""
    if integration_method not in VALID_INTEGRATION_METHODS:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Invalid integration_method '{integration_method}'. Must be one of: "
                   f"{', '.join(VALID_INTEGRATION_METHODS)}"
        )


def validate_partition_strategy(partition_strategy: str) -> None:
    """400 for an unknown V2 package partition strategy.

    Bodies are permuted once at job creation so contiguous package slicing yields
    the requested shape: 'linear' (file order), 'random' (seeded shuffle),
    'spatial' (Morton/Z-order locality) or 'orb' (recursive bisection, one
    box per package). See v2.partitioning."""
    if partition_strategy not in VALID_PARTITION_STRATEGIES:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid partition_strategy '{partition_strategy}'. "
                f"Must be one of: {', '.join(VALID_PARTITION_STRATEGIES)}"
            )
        )


def calculate_job_progress(job: Job, completed_packages_count: Optional[int] = None) -> float:
    """The job's completion percentage. completed_packages_count: pre-counted
    (no N+1 queries when listing several jobs)."""
    if job.status == JobStatus.COMPLETED.value:
        return 100.0
    elif job.status in [JobStatus.PENDING.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value]:
        return 0.0

    # For monolithic jobs (num_packages=0), calculate based on epochs only
    if job.num_packages == 0:
        if job.num_epochs == 0:
            return 0.0
        return min(100.0, (job.current_epoch / job.num_epochs) * 100.0)

    # For distributed jobs, calculate based on current epoch and packages completed
    total_operations = job.num_epochs * job.num_packages

    if total_operations == 0:
        return 0.0

    completed_operations = job.current_epoch * job.num_packages

    # Add completed packages in current epoch
    if job.current_epoch < job.num_epochs:
        if completed_packages_count is not None:
            # Use pre-calculated count if provided
            completed_packages = completed_packages_count
        else:
            # Fall back to individual query (for single job endpoints)
            # computed packages of the epoch in progress: TO_MERGE (MERGED
            # only briefly, as the epoch is then recorded and moves on)
            completed_packages = Package.select().where(
                Package.job == job,
                Package.epoch_num == job.current_epoch + 1,
                Package.status.in_([PackageStatus.TO_MERGE.value, PackageStatus.MERGED.value])
            ).count()
        completed_operations += completed_packages

    return min(100.0, (completed_operations / total_operations) * 100.0)


def is_combined_results_file(file_key: str) -> bool:
    """Check if file key points to a combined results file (not a single epoch).

    Combined files have path: job_id/{job_id}/combined_results.csv
    V2 files have path: v2/job_id/{job_id}/final_results.csv
    Single epoch files have path: job_id/{job_id}/epoch_num/{epoch}/epoch_results.csv

    This is critical for handling cases where combination failed but job is marked COMPLETED.
    """
    if not file_key:
        return False
    # V2 jobs have final_results.csv
    if file_key.endswith('/final_results.csv') and file_key.startswith('v2/'):
        return True
    # V1 combined results
    return file_key.endswith('/combined_results.csv') and 'epoch_num' not in file_key


def get_barnes_hut_theta(force_algorithm: str, barnes_hut_theta: float) -> float:
    """The theta to record: the given one for Barnes-Hut, the default for direct
    summation."""
    if force_algorithm == "barnes_hut":
        return barnes_hut_theta
    return DEFAULT_THETA
