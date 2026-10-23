# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
V2 API Router - FastAPI endpoints for V2 distributed worker.

This router provides endpoints for:
- Creating V2 jobs (with file upload support like V1)
- Checking job status (from both PostgreSQL and Redis)
- Recovering jobs from snapshots
- Cancelling jobs
"""

import json
import math
import threading
from typing import Dict, List, Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import prometheus_metrics

# from auth, not api: importing api here would be circular
from auth import get_current_user
from config import settings
from constants import JOB_NAME_MAX, MAX_EPOCHS, SOFTENING_SQ
from db_threads import ClosingRoute
from integrators import INTEGRATION_METHOD_HELP
from logging_config import setup_logging
from models import Job, User, utcnow
from schemas import JobStatus, JobType
from services.comparison_cache import delete_persistent_metrics, invalidate_job_cache
from utils.input_files import NameConflict, check_file_source, resolve_input_file
from utils.job_helpers import validate_partition_strategy
from utils.job_submission import (
    ADAPTIVE_DT_MAX_HELP,
    ADAPTIVE_DT_MIN_HELP,
    ADAPTIVE_ETA_HELP,
    ADAPTIVE_HELP,
    FORCE_ALGORITHM_HELP,
    JOB_DESCRIPTION_HELP,
    JOB_NAME_HELP,
    SOFTENING_HELP,
    THETA_HELP,
    job_fields,
    validate_adaptive_bounds,
    validate_job_parameters,
)

from .file_service_v2 import (
    EPOCH_RESULTS_BUCKET,
    delete_snapshots,
    download_snapshot,
    final_results_csv,
    get_final_results_key,
    get_s3_client,
    list_snapshots,
)
from .orchestrator_v2 import RecoveryConflict, get_orchestrator
from .partitioning import DEFAULT_PARTITION_SEED, DEFAULT_PARTITION_STRATEGY
from .recovery import recover_v2_job
from .redis_service import get_redis_service


logger = setup_logging('api-v2')
router = APIRouter(route_class=ClosingRoute)
# Handlers doing blocking Redis/PostgreSQL/S3 work are plain `def`: FastAPI runs
# them in its threadpool, so they don't stall the event loop (one API process).

# Configuration
INPUT_FILES_BUCKET = settings.s3.input_files_bucket
DEFAULT_TIME_STEP = 0.0001

# Pydantic Models for API

class CreateJobResponse(BaseModel):
    """Response body for job creation."""
    job_id: int
    message: str
    file_key: str
    num_bodies: int


class JobStatusResponse(BaseModel):
    """Response body for job status."""
    job_id: int
    status: str
    current_epoch: int
    num_epochs: int
    total_packages: int
    snapshot_interval: int
    progress_percentage: float
    results_file_key: Optional[str] = None
    job_type: str = "distributed_v2"
    metadata: Optional[dict] = None
    # Fencing generation of the Redis meta (changes on every recovery, a large
    # number after the meta was rebuilt from PostgreSQL, so not a count).
    # None once Redis has no meta.
    generation: Optional[int] = None
    # epochs by a(0) source: returned / fallback. None if not an accel job
    accel_sources: Optional[dict] = None
    # Number of recoveries (PostgreSQL Job.recovery_count).
    recovery_count: Optional[int] = None


class RecoverJobResponse(BaseModel):
    """Response body for job recovery."""
    job_id: int
    recovered_epoch: int
    message: str


class HealthResponse(BaseModel):
    """Response body for health check."""
    status: str
    redis: str
    version: str = "2.0.0"


# API Endpoints

@router.get("/health", response_model=HealthResponse)
def health_check():
    """Check V2 service health including Redis connectivity."""
    redis = get_redis_service()
    redis_status = "connected" if redis.ping() else "disconnected"

    if redis_status != "connected":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis is not available"
        )

    return HealthResponse(
        status="healthy",
        redis=redis_status
    )


@router.post("/jobs", response_model=CreateJobResponse, status_code=status.HTTP_201_CREATED)
def create_job(
    current_user: User = Depends(get_current_user),
    file: Optional[UploadFile] = File(None, description="CSV file with body data"),
    file_id: Optional[int] = Form(None, gt=0, description="Use file from library by ID"),
    user_friendly_name: Optional[str] = Form(None, description="User-friendly name for uploaded file"),
    description: Optional[str] = Form(None, description="Description for uploaded file"),
    force_new: bool = Form(False, description="Force create new file even if name exists"),
    num_epochs: int = Form(100, gt=0, le=MAX_EPOCHS, description="Number of simulation epochs"),
    package_size: int = Form(1000, gt=0, description="Bodies per worker package"),
    time_step: float = Form(DEFAULT_TIME_STEP, allow_inf_nan=False, gt=0.0, description="Physics time step"),
    snapshot_interval: int = Form(0, ge=0, description="Save snapshot every N epochs (0=final only)"),
    force_algorithm: str = Form("direct", description=FORCE_ALGORITHM_HELP),
    barnes_hut_theta: float = Form(0.5, allow_inf_nan=False, ge=0.1, le=2.0, description=THETA_HELP),
    softening_sq: float = Form(SOFTENING_SQ, allow_inf_nan=False, gt=0.0, description=SOFTENING_HELP),
    integration_method: str = Form("symplectic_euler", description=INTEGRATION_METHOD_HELP),
    gpu_enabled: bool = Form(
        False,
        description="Enable GPU acceleration. Requires GPU workers to be deployed. "
                   "Note: GPU does not support Barnes-Hut algorithm."
    ),
    adaptive_enabled: bool = Form(
        False, description=ADAPTIVE_HELP + ". The timestep follows the maximum acceleration each epoch."),
    adaptive_eta: float = Form(0.02, allow_inf_nan=False, gt=0.0, le=1.0, description=ADAPTIVE_ETA_HELP),
    adaptive_dt_min: float = Form(0.0, allow_inf_nan=False, ge=0.0, description=ADAPTIVE_DT_MIN_HELP),
    adaptive_dt_max: float = Form(0.0, allow_inf_nan=False, ge=0.0, description=ADAPTIVE_DT_MAX_HELP),
    job_description: Optional[str] = Form(None, max_length=1000, description=JOB_DESCRIPTION_HELP),
    job_name: Optional[str] = Form(None, max_length=JOB_NAME_MAX, description=JOB_NAME_HELP),
    partition_strategy: str = Form(
        DEFAULT_PARTITION_STRATEGY,
        description="How bodies are split into packages: 'linear' (file order, "
                    "default), 'random' (seeded shuffle for load balance), "
                    "'spatial' (Morton/Z-order locality) or 'orb' (orthogonal "
                    "recursive bisection, cuts on the package boundaries). "
                    "Permute-once at job creation. Results are identical for "
                    "single-stage methods and gvv_accel, for the other predicted "
                    "multi-stage methods the partition changes the error."
    ),
    partition_seed: int = Form(
        DEFAULT_PARTITION_SEED,
        ge=0,
        description="Seed for the 'random' partition strategy (ignored otherwise)."
    ),
):
    """Create a new V2 distributed simulation job.

    Supports two file input methods (same as V1):
    - file: Upload a new file
    - file_id: Use existing file from library

    The job will be:
    1. Persisted to PostgreSQL (for listing and history)
    2. Body data stored in Redis (binary format for fast access)
    3. Initial epoch tasks queued in Redis Stream
    4. Workers automatically pick up tasks
    """
    logger.info(f"V2 job creation request from user {current_user.username}: "
                f"epochs={num_epochs}, package_size={package_size}")

    check_file_source(file, file_id)
    validate_job_parameters(num_epochs, snapshot_interval, force_algorithm, integration_method, gpu_enabled)
    validate_adaptive_bounds(adaptive_dt_min, adaptive_dt_max)
    validate_partition_strategy(partition_strategy)

    try:
        try:
            source = resolve_input_file(file, file_id, user_friendly_name, description, force_new)
        except NameConflict as conflict:
            return conflict.response
        bodies: List[Dict] = [b.model_dump() for b in source.bodies]
        file_key = source.file_key
        n_bodies = len(bodies)
        total_packages = math.ceil(n_bodies / package_size)

        fields = job_fields(
            source, num_epochs=num_epochs, time_step=time_step, snapshot_interval=snapshot_interval,
            use_gpu=gpu_enabled, force_algorithm=force_algorithm, barnes_hut_theta=barnes_hut_theta,
            softening_sq=softening_sq, integration_method=integration_method,
            adaptive_enabled=adaptive_enabled, adaptive_eta=adaptive_eta, adaptive_dt_min=adaptive_dt_min,
            adaptive_dt_max=adaptive_dt_max, job_description=job_description, job_name=job_name)
        job = Job(
            package_size=package_size,
            num_packages=total_packages,
            job_type=JobType.DISTRIBUTED_V2.value,
            partition_strategy=partition_strategy,
            partition_seed=partition_seed,
            **fields,
        )
        job.save()
        prometheus_metrics.record_job_created()

        logger.info(f"Created V2 job {job.job_id} in PostgreSQL")

        try:
            orchestrator = get_orchestrator()
            orchestrator.create_job_with_bodies(
                job_id=job.job_id,
                bodies=bodies,
                num_epochs=num_epochs,
                time_step=time_step,
                package_size=package_size,
                snapshot_interval=snapshot_interval,
                input_bucket=INPUT_FILES_BUCKET,
                input_file_key=file_key,
                force_algorithm=force_algorithm,
                barnes_hut_theta=fields["barnes_hut_theta"],
                softening_sq=softening_sq,
                integration_method=integration_method,
                adaptive_enabled=adaptive_enabled,
                adaptive_eta=adaptive_eta,
                adaptive_dt_min=adaptive_dt_min,
                adaptive_dt_max=adaptive_dt_max,
                gpu_enabled=gpu_enabled,
                partition_strategy=partition_strategy,
                partition_seed=partition_seed,
            )

            logger.info(f"V2 job {job.job_id} initialized in Redis "
                       f"(algorithm={force_algorithm}" +
                       (f", theta={barnes_hut_theta}" if force_algorithm == "barnes_hut" else "") +
                       f", integration={integration_method}" +
                       (", GPU enabled" if gpu_enabled else "") + ")")

        except Exception as e:
            # Rollback PostgreSQL job if Redis fails
            logger.error(f"Failed to initialize V2 job in Redis: {e}")
            job.delete_instance()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to initialize job: {str(e)}"
            )

        return CreateJobResponse(
            job_id=job.job_id,
            message=f"V2 job created: {n_bodies} bodies, {num_epochs:,} epochs, {total_packages} packages",
            file_key=file_key,
            num_bodies=n_bodies
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Job creation failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create job: {str(e)}"
        )


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: int, current_user: User = Depends(get_current_user)):
    """Get status of a V2 job.

    Checks both PostgreSQL (job record) and Redis (live progress).
    """
    # First check PostgreSQL for job existence and basic info
    job = _v2_job(job_id)

    orchestrator = get_orchestrator()
    redis_status = orchestrator.get_job_status(job_id)

    if redis_status:
        # Use Redis data for live progress
        current_epoch = redis_status['current_epoch']
        num_epochs = redis_status['num_epochs']
        progress = (current_epoch / num_epochs * 100) if num_epochs > 0 else 0

        return JobStatusResponse(
            job_id=job_id,
            status=redis_status['status'],
            current_epoch=current_epoch,
            num_epochs=num_epochs,
            total_packages=redis_status['total_packages'],
            snapshot_interval=redis_status['snapshot_interval'],
            progress_percentage=round(progress, 2),
            results_file_key=redis_status.get('results_file_key'),
            job_type=JobType.DISTRIBUTED_V2.value,
            generation=redis_status.get('generation'),
            accel_sources=redis_status.get('accel_sources'),
            recovery_count=job.recovery_count,
        )
    else:
        # Fallback to PostgreSQL data (Redis data may have been cleaned up)
        results_file_key = None
        if job.status == JobStatus.COMPLETED.value:
            results_file_key = job.results_file_key or f"v2/job_id/{job_id}/final_results.csv"

        progress = (job.current_epoch / job.num_epochs * 100) if job.num_epochs > 0 else 0

        return JobStatusResponse(
            job_id=job_id,
            status=job.status,
            current_epoch=job.current_epoch,
            num_epochs=job.num_epochs,
            total_packages=job.num_packages,
            snapshot_interval=job.snapshot_interval,
            progress_percentage=round(progress, 2),
            results_file_key=results_file_key,
            job_type=JobType.DISTRIBUTED_V2.value,
            metadata=json.loads(job.metadata_json) if job.metadata_json else None,
            recovery_count=job.recovery_count,
        )


def _v2_job(job_id: int) -> Job:
    """The job, or 404. 400 if it is not a V2 job (its state is not in Redis,
    so the V2 lifecycle calls would report it as lost)."""
    job = Job.get_or_none(Job.job_id == job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    if job.job_type != JobType.DISTRIBUTED_V2.value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Job {job_id} is not a V2 job (type: {job.job_type})")
    return job


@router.post("/jobs/{job_id}/cancel", response_model=dict)
def cancel_job(job_id: int, current_user: User = Depends(get_current_user)):
    """Cancel a running V2 job."""
    job = _v2_job(job_id)

    cancellable = (JobStatus.PENDING.value, JobStatus.IN_PROGRESS.value, JobStatus.PAUSED.value)
    if job.status not in cancellable:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel job with status '{job.status}'. Only PENDING, IN_PROGRESS or PAUSED "
                   f"jobs can be cancelled."
        )

    # Redis first (the workers follow it): a job that finished there, but whose
    # completion has not reached PostgreSQL yet, is not turned into CANCELLED.
    orchestrator = get_orchestrator()
    if not orchestrator.cancel_job(job_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} has already finished (COMPLETED, FAILED or CANCELLED)"
        )

    # Conditional, not job.save(): never over a final state written meanwhile
    updated = Job.update(status=JobStatus.CANCELLED.value, updated_at=utcnow()).where(
        (Job.job_id == job_id) & (Job.status.in_(cancellable))
    ).execute()
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} changed status meanwhile, not cancelled in PostgreSQL"
        )

    return {"message": f"Job {job_id} cancelled", "job_id": job_id, "status": JobStatus.CANCELLED.value}


@router.post("/jobs/{job_id}/pause", response_model=dict)
def pause_job(job_id: int, current_user: User = Depends(get_current_user)):
    """Pause a running V2 job."""
    job = _v2_job(job_id)

    pausable = (JobStatus.PENDING.value, JobStatus.IN_PROGRESS.value)
    if job.status not in pausable:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Can only pause PENDING or IN_PROGRESS jobs (current: {job.status})"
        )

    orchestrator = get_orchestrator()
    if not orchestrator.pause_job(job_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} cannot be paused: it has finished, or its Redis state is gone "
                   f"(recover it from its latest snapshot)"
        )

    updated = Job.update(status=JobStatus.PAUSED.value, updated_at=utcnow()).where(
        (Job.job_id == job_id) & (Job.status.in_(pausable))
    ).execute()
    if not updated:
        logger.warning(f"Job {job_id} paused in Redis, but its PostgreSQL status changed meanwhile")

    return {"message": f"Job {job_id} paused at epoch {job.current_epoch}", "job_id": job_id}


@router.post("/jobs/{job_id}/resume", response_model=dict)
def resume_job(job_id: int, current_user: User = Depends(get_current_user)):
    """Resume a paused V2 job.

    PostgreSQL is set IN_PROGRESS only once Redis has resumed it. A job that
    cannot be resumed (its Redis state is gone, or no epoch is left) stays
    PAUSED and the call returns 409.
    """
    job = _v2_job(job_id)

    if job.status != JobStatus.PAUSED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Can only resume PAUSED jobs (current: {job.status})"
        )

    orchestrator = get_orchestrator()
    if not orchestrator.resume_job(job_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} cannot be resumed: its Redis state is gone or it has no epoch "
                   f"left. Recover it from its latest snapshot (POST /jobs/{job_id}/recover)."
        )

    Job.update(
        status=JobStatus.IN_PROGRESS.value,
        worker_id=None,
        leased_at=None,
        updated_at=utcnow(),
    ).where((Job.job_id == job_id) & (Job.status == JobStatus.PAUSED.value)).execute()

    return {"message": f"Job {job_id} resumed from epoch {job.current_epoch}", "job_id": job_id}


@router.post("/jobs/{job_id}/recover", response_model=RecoverJobResponse)
def recover_job(job_id: int, force: bool = False, current_user: User = Depends(get_current_user)):
    """Recover a V2 job from its latest S3 snapshot, or from its input file
    when it has no snapshot yet (recovered_epoch 0: it restarts at epoch 1).

    Can recover even if Redis metadata is lost (uses PostgreSQL as fallback).
    A COMPLETED job is refused (409) unless force=true: recovering it reruns
    the epochs after its latest snapshot (none, when that is the final one: a
    worker then republishes the final epoch) and completes it again
    (completed_at is cleared, so the second completion is recorded).
    A PAUSED job is resumed by its recovery. A CANCELLED job is refused (409),
    as is a recovery while another of the same job runs, and one a cancel
    overtakes (the cancel wins). 503 when the object store fails (nothing is
    changed then).
    """
    job = _v2_job(job_id)

    if job.status == JobStatus.COMPLETED.value and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} is COMPLETED, pass force=true to recover and rerun it anyway"
        )

    if job.status == JobStatus.CANCELLED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} is CANCELLED, a cancelled job is not recovered"
        )

    was_completed = job.status == JobStatus.COMPLETED.value   # before the row is updated
    try:
        # the PostgreSQL row is the fallback for the metadata
        recovered_epoch = recover_v2_job(job_id, pg_job=job)

        if recovered_epoch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job {job_id} cannot be recovered: no usable snapshot, input file or metadata"
            )

        # A recovered job gets new snapshots: drop the metrics cached for the
        # old run. Only a completed job has persisted metrics, and
        # their S3 delete runs in the background: with the object store down it
        # would otherwise hold the recovery response through every retry.
        try:
            invalidate_job_cache(job_id)
            if was_completed:
                def _drop_persisted():
                    try:
                        delete_persistent_metrics(job_id)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"Job {job_id} recovered, but its persisted metrics were not deleted: {e}")
                threading.Thread(target=_drop_persisted, name=f"drop-metrics-{job_id}", daemon=True).start()
        except Exception as e:  # noqa: BLE001 — the recovery itself succeeded
            logger.warning(f"Job {job_id} recovered, but its cached metrics were not dropped: {e}")

        return RecoverJobResponse(
            job_id=job_id,
            recovered_epoch=recovered_epoch,
            message=f"Job {job_id} recovered from " +
                    (f"epoch {recovered_epoch}" if recovered_epoch else "its input (no snapshot yet)")
        )

    except HTTPException:
        raise
    except RecoveryConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Job {job_id} cannot be recovered: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"Object store unavailable, job {job_id} not recovered (retry): {e}")
    except BotoCoreError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"Object store unavailable, job {job_id} not recovered (retry): {e}")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to recover job: {str(e)}"
        )


@router.delete("/jobs/{job_id}", response_model=dict)
def cleanup_job(job_id: int, current_user: User = Depends(get_current_user)):
    """Clean up Redis data for a V2 job.

    Only allows cleanup of completed, failed, or cancelled jobs.
    Does NOT delete PostgreSQL record or S3 files (use /permanent for that).
    """
    job = _v2_job(job_id)

    # Only allow cleanup of terminal states
    terminal_states = (JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value)
    if job.status not in terminal_states:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cleanup job with status '{job.status}'. Job must be COMPLETED, FAILED, or CANCELLED."
        )

    orchestrator = get_orchestrator()
    orchestrator.cleanup_completed_job(job_id)

    return {"message": f"Redis data for job {job_id} cleaned up", "job_id": job_id}


@router.delete("/jobs/{job_id}/permanent", response_model=dict)
def delete_job_permanent(job_id: int, current_user: User = Depends(get_current_user)):
    """Permanently delete a V2 job including all data.

    Removes:
    - Redis data (body state, metadata, epoch keys)
    - S3 snapshots and results
    - PostgreSQL job record
    """

    job = _v2_job(job_id)

    # Only allow deletion of terminal states
    terminal_states = (JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value)
    if job.status not in terminal_states:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete job with status '{job.status}'. Job must be COMPLETED, FAILED, or CANCELLED."
        )

    deleted_files = 0
    failed = []

    # 1. Clean up Redis data
    try:
        orchestrator = get_orchestrator()
        orchestrator.cleanup_completed_job(job_id)
    except Exception as e:
        logger.warning(f"Failed to cleanup Redis for job {job_id}: {e}")
        failed.append("Redis state")

    # 2. Delete S3 snapshots
    try:
        delete_snapshots(job_id)
        deleted_files += 1  # all of a job's snapshots count as one
    except Exception as e:
        logger.warning(f"Failed to delete snapshots for job {job_id}: {e}")
        failed.append("snapshots")

    # 3. Delete final results CSV
    try:
        s3 = get_s3_client()
        results_key = get_final_results_key(job_id)
        s3.delete_object(Bucket=EPOCH_RESULTS_BUCKET, Key=results_key)
        deleted_files += 1
    except Exception as e:
        logger.warning(f"Failed to delete results CSV for job {job_id}: {e}")
        failed.append("results file")

    # The row is the only handle on the job's data: kept while any of it is
    # left, so the delete can be retried
    if failed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not delete the {', '.join(failed)} of job {job_id}, the job is kept, try again"
        )

    # 4. Delete PostgreSQL record
    try:
        job.delete_instance()
    except Exception as e:
        logger.error(f"Failed to delete PostgreSQL record for job {job_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete job record: {str(e)}"
        )

    return {
        "message": f"Job {job_id} permanently deleted",
        "job_id": job_id,
        "deleted_files": deleted_files
    }


@router.get("/jobs/{job_id}/results")
def download_results(job_id: int, current_user: User = Depends(get_current_user)):
    """Download final results for a completed V2 job."""

    job = _v2_job(job_id)

    if job.status != JobStatus.COMPLETED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job {job_id} is not completed (status: {job.status})"
        )

    # Stream the pre-generated CSV (one frame of N rows: never read whole)
    try:
        body = get_s3_client().get_object(Bucket=EPOCH_RESULTS_BUCKET, Key=get_final_results_key(job_id))['Body']
    except Exception:
        # Fall back to generating from latest snapshot
        logger.warning(f"Final CSV not found for job {job_id}, generating from snapshot")
        try:
            epochs = list_snapshots(job_id)
        except Exception as e:  # noqa: BLE001 — an outage, not a missing result
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail=f"Object storage unavailable: {e}")
        if not epochs:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No results available for job {job_id}"
            )

        csv_content = final_results_csv(download_snapshot(job_id, epochs[-1]), epochs[-1])
        content = _iter_encoded([csv_content])
    else:
        def stream_body():
            try:
                yield from body.iter_chunks(chunk_size=1 << 16)
            finally:
                body.close()
        content = stream_body()

    return StreamingResponse(
        content,
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename=v2_results_{job_id}.csv'}
    )


class IncompleteHistoryError(RuntimeError):
    """A snapshot or the final state of the job could not be read."""


HISTORY_FIELDS = ['epoch', 'n', 'idBody', 'm', 'x', 'y', 'z', 'vx', 'vy', 'vz']


def _iter_encoded(parts):
    """str parts as UTF-8 bytes, a part at a time."""
    for part in parts:
        yield part.encode('utf-8')


def iter_full_history_csv(job_id: int, completed: bool):
    """Every intermediate snapshot plus, for a completed job, the final
    state (written only as final_results.csv), once each and in epoch order,
    as CSV parts: one frame at a time is read and formatted, never the whole
    history. What can be checked before the first part is (LookupError if
    there is nothing to export, IncompleteHistoryError if the final state is
    unreadable). A snapshot that cannot be read raises IncompleteHistoryError
    when its turn comes, ending the export instead of leaving it out."""

    try:
        epochs = list_snapshots(job_id)
    except Exception as e:  # noqa: BLE001
        raise IncompleteHistoryError(f"job {job_id}: snapshots could not be listed ({e})")
    final_part = None
    if completed:
        try:
            raw = get_s3_client().get_object(Bucket=EPOCH_RESULTS_BUCKET,
                                             Key=get_final_results_key(job_id))['Body'].read().decode('utf-8')
        except Exception as e:  # noqa: BLE001
            raise IncompleteHistoryError(f"job {job_id}: final state unreadable ({e})")
        rows = raw.splitlines()[1:]
        del raw
        final_epoch = int(rows[0].split(',', 1)[0]) if rows else None
        if final_epoch is not None and final_epoch not in epochs:   # not already a binary snapshot
            final_part = "\r\n".join(rows) + "\r\n"
    if not epochs and final_part is None:
        raise LookupError(f"No snapshots available for job {job_id}")

    def parts():
        # Frames are formatted by final_results_csv (the csv module's output,
        # CRLF line ends, without per-body dicts) and joined without their headers
        yield ",".join(HISTORY_FIELDS) + "\r\n"
        for epoch in epochs:
            try:
                frame = final_results_csv(download_snapshot(job_id, epoch), epoch)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Failed to load snapshot {epoch} for job {job_id}: {e}")
                raise IncompleteHistoryError(f"job {job_id}: snapshots of epochs [{epoch}] could not be read")
            yield frame.split("\r\n", 1)[1]
        if final_part is not None:
            yield final_part
    return parts()


@router.get("/jobs/{job_id}/results/full-history")
def download_full_history(job_id: int, current_user: User = Depends(get_current_user)):
    """Download every snapshot of a V2 job and, once it has completed, its
    final state, as one CSV (iter_full_history_csv), streamed frame by frame.
    The handler returns (and its database connection) before the stream
    starts. A snapshot unreadable mid-stream aborts the response, so a
    client never gets a history silently missing a frame."""

    job = _v2_job(job_id)
    try:
        parts = iter_full_history_csv(job_id, job.status == JobStatus.COMPLETED.value)
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except IncompleteHistoryError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))

    return StreamingResponse(
        _iter_encoded(parts),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename=v2_full_history_{job_id}.csv'}
    )


@router.get("/jobs", response_model=dict)
def list_jobs(
    current_user: User = Depends(get_current_user),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = None
):
    """List V2 jobs with pagination.

    Returns jobs from PostgreSQL filtered by job_type='distributed_v2'.
    """
    query = Job.select().where(Job.job_type == JobType.DISTRIBUTED_V2.value)

    if status_filter:
        query = query.where(Job.status == status_filter)

    total = query.count()
    jobs = list(query.order_by(Job.created_at.desc()).paginate(page, size))

    return {
        "jobs": [
            {
                "job_id": j.job_id,
                "status": j.status,
                "current_epoch": j.current_epoch,
                "num_epochs": j.num_epochs,
                "total_packages": j.num_packages,
                "package_size": j.package_size,
                "snapshot_interval": j.snapshot_interval,
                "time_step": j.time_step,
                "filename": j.filename,
                "file_size": j.file_size,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "updated_at": j.updated_at.isoformat() if j.updated_at else None,
                "progress_percentage": round((j.current_epoch / j.num_epochs * 100) if j.num_epochs > 0 else 0, 2),
                "job_type": j.job_type,
                "force_algorithm": j.force_algorithm,
                "barnes_hut_theta": j.barnes_hut_theta,
                "softening_sq": j.softening_sq
            }
            for j in jobs
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if size > 0 else 0
    }


@router.get("/stats", response_model=dict)
def get_queue_stats(current_user: User = Depends(get_current_user)):
    """Get task queue statistics."""
    redis = get_redis_service()
    pending_info = redis.get_pending_info()

    return {
        "pending_tasks": pending_info.get('pending_count', 0),
        "consumers": pending_info.get('consumers', [])
    }
