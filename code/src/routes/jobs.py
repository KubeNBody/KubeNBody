# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Endpoints for job CRUD operations: submit, list, get, cancel, delete.
"""

import asyncio
import json
import math
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, UploadFile, status
from peewee import JOIN, fn
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_500_INTERNAL_SERVER_ERROR

import prometheus_metrics
from api_schemas import (
    FileUploadResponse,
    JobDescriptionUpdate,
    JobListResponse,
    JobNameUpdate,
    JobProgressResponse,
    JobResponse,
    JobSelectableResponse,
    PackageResponse,
    normalize_job_name,
)
from api_schemas import JobStatus as APIJobStatus
from async_file_service import EPOCH_RESULTS_BUCKET_NAME
from auth import get_current_user
from config import settings
from constants import JOB_NAME_MAX, MAX_EPOCHS, SOFTENING_SQ
from db_threads import ClosingRoute, closes_connection
from file_service import PARTIAL_RESULTS_BUCKET_NAME, delete_files_by_prefix
from integrators import INTEGRATION_METHOD_HELP
from logging_config import setup_logging
from models import DEFAULT_TIME_STEP, Job, Package, User, db, utcnow
from publisher import on_rabbitmq_channel, publish_persistent
from rabbitmq import GPU_DISTRIBUTED_QUEUE, GPU_MONOLITHIC_QUEUE, MONOLITHIC_QUEUE, NEW_QUEUE, BrokerUnavailable
from schemas import JobStatus, JobType, MonolithicJobMessage, PackageStatus, WorkerMessage
from utils.input_files import NameConflict, check_file_source, resolve_input_file
from utils.job_helpers import calculate_job_progress
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
from v1_epochs import queue_v1_epoch
from v2.api_v2 import delete_job_permanent as delete_v2_job_permanent


logger = setup_logging('api.jobs')

# Fire-and-forget tasks (S3 cleanup after a permanent delete), referenced here
_background_tasks: set = set()

router = APIRouter(prefix="/api/jobs", tags=["Jobs"], route_class=ClosingRoute)

INPUT_FILES_BUCKET = settings.s3.input_files_bucket


def _discard_unqueued_job(job_id: int) -> None:
    """A submission whose messages were not all published: delete its job and
    packages. Packages already queued are then dropped by the v1 workers (no
    package row to complete, so no merge), a monolithic message by the
    monolithic worker (no job row). Failing that, the job is marked FAILED so
    it does not wait IN_PROGRESS for messages that never come."""
    try:
        with db.atomic():
            Package.delete().where(Package.job == job_id).execute()
            Job.delete().where(Job.job_id == job_id).execute()
        logger.info(f"Deleted job {job_id} after its publish failed")
    except Exception as delete_error:
        logger.error(f"Failed to delete job {job_id} after its publish failed: {delete_error}")
        try:
            Job.update(status=JobStatus.FAILED.value, updated_at=utcnow()).where(
                Job.job_id == job_id).execute()
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to mark job {job_id} FAILED: {e}")


@router.post("", response_model=FileUploadResponse)
async def submit_job(
    file: Optional[UploadFile] = File(None, description="CSV file with body data"),
    file_id: Optional[int] = Form(None, gt=0, description="Use file from library by ID"),
    user_friendly_name: Optional[str] = Form(None, description="User-friendly name for uploaded file"),
    description: Optional[str] = Form(None, description="Description for uploaded file"),
    force_new: bool = Form(False, description="Force upload even if name conflicts"),
    num_epochs: int = Form(..., gt=0, le=MAX_EPOCHS),
    package_size: int = Form(None, gt=0, description="Required for distributed jobs, ignored for monolithic"),
    time_step: float = Form(DEFAULT_TIME_STEP, allow_inf_nan=False, gt=0.0),
    snapshot_interval: int = Form(5, gt=0),
    job_type: str = Form("distributed", description="Job type: 'distributed' or 'monolithic'"),
    use_gpu: bool = Form(
        False,
        description=(
            "Enable GPU acceleration using CUDA for physics computation. "
            "Requirements: a GPU worker must be running. GPU jobs go to dedicated queues "
            "(GPU_MONOLITHIC_QUEUE, GPU_DISTRIBUTED_QUEUE), so they do not block CPU jobs."
        )
    ),
    force_algorithm: str = Form("direct", description=FORCE_ALGORITHM_HELP),
    barnes_hut_theta: float = Form(0.5, allow_inf_nan=False, ge=0.1, le=2.0, description=THETA_HELP),
    softening_sq: float = Form(SOFTENING_SQ, allow_inf_nan=False, gt=0.0, description=SOFTENING_HELP),
    integration_method: str = Form("symplectic_euler", description=INTEGRATION_METHOD_HELP),
    adaptive_enabled: bool = Form(False, description=ADAPTIVE_HELP),
    adaptive_eta: float = Form(0.02, allow_inf_nan=False, gt=0, le=1.0, description=ADAPTIVE_ETA_HELP),
    adaptive_dt_min: float = Form(0.0, allow_inf_nan=False, ge=0, description=ADAPTIVE_DT_MIN_HELP),
    adaptive_dt_max: float = Form(0.0, allow_inf_nan=False, ge=0, description=ADAPTIVE_DT_MAX_HELP),
    job_description: Optional[str] = Form(None, max_length=1000, description=JOB_DESCRIPTION_HELP),
    job_name: Optional[str] = Form(None, max_length=JOB_NAME_MAX, description=JOB_NAME_HELP),
    current_user: User = Depends(get_current_user)
):
    """Submit a new N-body simulation job (upload a new file or use a library file).

    Job Types:
    - distributed: Split into packages, processed by multiple workers (good for large N)
    - monolithic: Single execution, all epochs in one worker (good for large epochs, small N)

    File Options:
    - file + user_friendly_name: Upload new file with specific name
    - file (no name): Upload new file with auto-generated name
    - file_id: Use existing file from library
    """
    logger.info(f"Received job submission request: job_type={job_type}")

    check_file_source(file, file_id)
    validate_job_parameters(num_epochs, snapshot_interval, force_algorithm, integration_method, use_gpu)
    validate_adaptive_bounds(adaptive_dt_min, adaptive_dt_max)
    try:
        job_type_enum = JobType(job_type.lower())
    except ValueError:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Invalid job_type '{job_type}'. Must be 'distributed' or 'monolithic'"
        )
    if job_type_enum not in (JobType.DISTRIBUTED, JobType.MONOLITHIC):
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"job_type '{job_type}' is not submitted here: V2 jobs go to POST /api/v2/jobs"
        )
    if job_type_enum != JobType.MONOLITHIC and (package_size is None or package_size <= 0):
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="package_size is required and must be > 0 for distributed jobs"
        )

    try:
        try:
            # PostgreSQL, S3 and parsing: in a thread, off the event loop
            source = await asyncio.to_thread(
                closes_connection(resolve_input_file), file, file_id,
                user_friendly_name, description, force_new)
        except NameConflict as conflict:
            return conflict.response
        bodies, file_key = source.bodies, source.file_key
        # the v1 orchestrator chooses each epoch's step from the adaptive fields
        fields = job_fields(
            source, num_epochs=num_epochs, time_step=time_step, snapshot_interval=snapshot_interval,
            use_gpu=use_gpu, force_algorithm=force_algorithm, barnes_hut_theta=barnes_hut_theta,
            softening_sq=softening_sq, integration_method=integration_method,
            adaptive_enabled=adaptive_enabled, adaptive_eta=adaptive_eta, adaptive_dt_min=adaptive_dt_min,
            adaptive_dt_max=adaptive_dt_max, job_description=job_description, job_name=job_name)
        theta = fields["barnes_hut_theta"]

        if job_type_enum == JobType.MONOLITHIC:
            logger.info(f"Creating MONOLITHIC job: N={len(bodies)}, epochs={num_epochs}")

            def create_monolithic():
                job = Job(package_size=0, num_packages=0, job_type=JobType.MONOLITHIC.value, **fields)
                job.save()
                return job
            job = await asyncio.to_thread(closes_connection(create_monolithic))
            prometheus_metrics.record_job_created()

            logger.info(f"Created monolithic job {job.job_id} (GPU: {use_gpu}, algorithm: {force_algorithm}, "
                        f"integration: {integration_method}, adaptive: {adaptive_enabled})")

            monolithic_message = MonolithicJobMessage(
                job_id=job.job_id,
                bucket=INPUT_FILES_BUCKET,
                file_key=file_key,
                num_of_epochs=num_epochs,
                time_step=time_step,
                snapshot_interval=snapshot_interval,
                gpu_enabled=use_gpu,
                force_algorithm=force_algorithm,
                barnes_hut_theta=theta,
                softening_sq=softening_sq,
                integration_method=integration_method,
                adaptive_enabled=adaptive_enabled,
                adaptive_eta=adaptive_eta,
                adaptive_dt_min=adaptive_dt_min,
                adaptive_dt_max=adaptive_dt_max,
            )
            target_queue = GPU_MONOLITHIC_QUEUE if use_gpu else MONOLITHIC_QUEUE
            messages = [(target_queue, monolithic_message.to_json())]
            response = FileUploadResponse(
                job_id=job.job_id,
                message=f"Monolithic job submitted: {len(bodies)} bodies, {num_epochs:,} epochs",
                file_key=file_key,
                num_bodies=len(bodies)
            )

        else:
            logger.info(f"Creating DISTRIBUTED job: N={len(bodies)}, epochs={num_epochs}")
            num_packages = math.ceil(len(bodies) / package_size)

            def create_distributed():
                # Job and packages in one transaction
                with db.atomic():
                    job = Job(package_size=package_size, num_packages=num_packages,
                              job_type=JobType.DISTRIBUTED.value, **fields)
                    job.save()
                    package_ids = []
                    for i in range(1, num_packages + 1):
                        package = Package(package_number=i, epoch_num=1, job=job,
                                          status=PackageStatus.IN_PROGRESS.value)
                        package.save()
                        package_ids.append(package.package_id)
                return job, package_ids
            job, package_ids = await asyncio.to_thread(closes_connection(create_distributed))
            prometheus_metrics.record_job_created()

            logger.info(f"Created distributed job {job.job_id} with {num_packages} packages (algorithm: "
                        f"{force_algorithm}, integration: {integration_method})")

            target_queue = GPU_DISTRIBUTED_QUEUE if use_gpu else NEW_QUEUE
            messages = [(target_queue, WorkerMessage.for_package(
                job, 1, i, package_id, INPUT_FILES_BUCKET, file_key, time_step).to_json())
                for i, package_id in enumerate(package_ids, start=1)]
            response = FileUploadResponse(
                job_id=job.job_id,
                message=f"Distributed job submitted: {len(bodies)} bodies, {num_packages} packages",
                file_key=file_key,
                num_bodies=len(bodies)
            )

        try:
            await publish_persistent(messages)
            logger.info(f"Published {len(messages)} message(s) of job {job.job_id} to {target_queue} (GPU: {use_gpu})")
        except Exception as publish_error:
            logger.error(f"Failed to publish job {job.job_id} to queue: {publish_error}")
            await asyncio.to_thread(closes_connection(_discard_unqueued_job), job.job_id)
            # 503 (retry later) when the broker is down, not a server bug
            raise HTTPException(
                status_code=503 if isinstance(publish_error, BrokerUnavailable) else HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to publish job to queue: {str(publish_error)}"
            )
        return response

    except ValueError as e:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to submit job: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to submit job")


def _job_to_response(job, completed_count=None) -> JobResponse:
    """The API view of a job (list, detail and description update).
    completed_count: the job's completed packages, if already counted."""
    return JobResponse(
        job_id=job.job_id,
        status=APIJobStatus(job.status),
        num_epochs=job.num_epochs,
        current_epoch=job.current_epoch,
        package_size=job.package_size,
        num_packages=job.num_packages,
        input_file_key=job.input_file_key,
        results_file_key=job.results_file_key,
        progress_percentage=calculate_job_progress(job, completed_count),
        snapshot_interval=job.snapshot_interval,
        time_step=job.time_step,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        retry_count=job.retry_count,
        recovery_count=job.recovery_count,
        lease_owner=job.worker_id,
        filename=job.filename,
        file_size=job.file_size,
        job_type=job.job_type,
        gpu_enabled=job.gpu_enabled,
        force_algorithm=job.force_algorithm,
        barnes_hut_theta=job.barnes_hut_theta,
        softening_sq=job.softening_sq,
        integration_method=job.integration_method,
        adaptive_enabled=job.adaptive_enabled,
        current_simulation_time=job.current_simulation_time,
        metadata=json.loads(job.metadata_json) if job.metadata_json else None,
        description=job.description,
        name=job.name,
        partition_strategy=job.partition_strategy,
        partition_seed=job.partition_seed,
    )


@router.get("/selectable", response_model=List[JobSelectableResponse])
def list_selectable_jobs(
    status: Optional[APIJobStatus] = None,
    current_user: User = Depends(get_current_user),
):
    """Return all jobs with minimal fields for selection dropdowns (e.g. compare page).

    Unpaginated by design - the UI needs to list ALL jobs at once. No expensive joins.
    Use the paginated /api/jobs endpoint for table views.
    """
    try:
        query = Job.select(
            Job.job_id, Job.status, Job.job_type, Job.filename,
            Job.num_epochs, Job.time_step, Job.gpu_enabled,
            Job.force_algorithm, Job.integration_method,
            Job.softening_sq, Job.description, Job.name, Job.created_at,
        )
        if status:
            query = query.where(Job.status == status.value)
        query = query.order_by(Job.created_at.desc())

        return [
            JobSelectableResponse(
                job_id=j.job_id,
                status=j.status,
                job_type=j.job_type,
                filename=j.filename,
                num_epochs=j.num_epochs,
                time_step=j.time_step,
                gpu_enabled=j.gpu_enabled,
                force_algorithm=j.force_algorithm,
                integration_method=j.integration_method,
                softening_sq=j.softening_sq if j.softening_sq is not None else SOFTENING_SQ,
                description=j.description,
                name=j.name,
                created_at=j.created_at,
            )
            for j in query
        ]
    except Exception as e:
        logger.error(f"Failed to list selectable jobs: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to list selectable jobs")


@router.get("", response_model=JobListResponse)
def list_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[APIJobStatus] = None,
    job_type: Optional[str] = Query(None, description="Filter by job type: distributed, distributed_v2, or monolithic"),
    integration_method: Optional[str] = Query(None, description="Filter by integration method (one of the "
                                                                "methods a job can be submitted with, e.g. "
                                                                "velocity_verlet, rk4, forest_ruth, or a "
                                                                "GPU-variant name)"),
    search: Optional[str] = Query(None, description="Substring match across job ID, filename, name and description"),
    current_user: User = Depends(get_current_user)
):
    """List all jobs with pagination - optimized to avoid N+1 queries."""
    try:
        # Build a reusable search predicate so both the count and the paginated
        # query apply identical filters. Matches numeric job_id exactly (when the
        # input parses as int) OR a case-insensitive substring of filename /
        # description.
        def _apply_filters(q):
            if status:
                q = q.where(Job.status == status.value)
            if job_type:
                q = q.where(Job.job_type == job_type)
            if integration_method:
                q = q.where(Job.integration_method == integration_method)
            if search:
                term = search.strip()
                if term:
                    # peewee .contains() emits ILIKE on Postgres. NULL columns
                    # safely return NULL (treated as false in WHERE).
                    clauses = [
                        Job.filename.contains(term),
                        Job.description.contains(term),
                        Job.name.contains(term),
                    ]
                    if term.isdigit():
                        clauses.append(Job.job_id == int(term))
                    pred = clauses[0]
                    for c in clauses[1:]:
                        pred = pred | c
                    q = q.where(pred)
            return q

        base_query = _apply_filters(Job.select())
        total = base_query.count()

        pages = math.ceil(total / page_size) if total > 0 else 1

        offset = (page - 1) * page_size

        jobs_with_counts = (
            Job.select(
                Job,
                fn.COALESCE(
                    fn.COUNT(Package.package_id), 0
                ).alias('completed_count')
            )
            .join(
                Package,
                JOIN.LEFT_OUTER,
                on=(
                    (Package.job == Job.job_id) &
                    # computed packages of the epoch in progress (see calculate_job_progress)
                    (Package.status.in_([PackageStatus.TO_MERGE.value, PackageStatus.MERGED.value])) &
                    (Package.epoch_num == (Job.current_epoch + 1))
                )
            )
        )

        jobs_with_counts = _apply_filters(jobs_with_counts)

        jobs_with_counts = (
            jobs_with_counts
            .group_by(Job)
            .order_by(Job.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )

        job_responses = []
        for job_data in jobs_with_counts:
            job = job_data
            completed_count = getattr(job_data, 'completed_count', 0)

            job_responses.append(_job_to_response(job, completed_count))

        return JobListResponse(
            jobs=job_responses,
            total=total,
            page=page,
            page_size=page_size,
            pages=pages
        )

    except Exception as e:
        logger.error(f"Failed to list jobs: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to list jobs")


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: int, current_user: User = Depends(get_current_user)):
    """Get detailed information about a specific job."""
    try:
        job = Job.get(Job.job_id == job_id)

        return _job_to_response(job)

    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    except Exception as e:
        logger.error(f"Failed to get job {job_id}: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get job")


@router.patch("/{job_id}/description", response_model=JobResponse)
def update_job_description(
    job_id: int,
    body: JobDescriptionUpdate,
    current_user: User = Depends(get_current_user),
):
    """Update the user-supplied description for a job.

    Editable in any job state. Pass `description: null` to clear it. The
    request enforces a 1000-character limit via the Pydantic schema.
    """
    new_desc = body.description.strip() if body.description else None
    if new_desc == "":
        new_desc = None

    # One column, not job.save(): rewriting the row read here would revert a
    # worker's concurrent status / epoch / results / lease write. updated_at
    # is left alone: it is the watchdog's progress signal and v1's completion
    # time, which a metadata edit must not move.
    if not Job.update(description=new_desc).where(Job.job_id == job_id).execute():
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    try:
        job = Job.get(Job.job_id == job_id)
    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")

    return _job_to_response(job)


@router.patch("/{job_id}/name", response_model=JobResponse)
def update_job_name(
    job_id: int,
    body: JobNameUpdate,
    current_user: User = Depends(get_current_user),
):
    """Set or clear (`name: null`) a job's short title, in any job state."""
    # One column, as for the description: see update_job_description
    if not Job.update(name=normalize_job_name(body.name)).where(Job.job_id == job_id).execute():
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    try:
        job = Job.get(Job.job_id == job_id)
    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")

    return _job_to_response(job)


@router.get("/{job_id}/status")
def get_job_status(job_id: int, current_user: User = Depends(get_current_user)):
    """Get quick status check for a job.

    Also carries the epoch counters, job type, retry/recovery counts and the
    monolithic lease owner, so a poller (e.g. a fault-injection driver) needs one
    cheap request per sample for either engine.
    """
    try:
        job = Job.get(Job.job_id == job_id)
        return {
            "job_id": job.job_id,
            "status": job.status,
            "progress": calculate_job_progress(job),
            "current_epoch": job.current_epoch,
            "num_epochs": job.num_epochs,
            "job_type": job.job_type,
            "retry_count": job.retry_count,
            "recovery_count": job.recovery_count,
            "lease_owner": job.worker_id,
        }
    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")


@router.get("/{job_id}/progress", response_model=JobProgressResponse)
def get_job_progress(job_id: int, current_user: User = Depends(get_current_user)):
    """Get detailed progress information for a job."""
    try:
        job = Job.get(Job.job_id == job_id)

        packages_completed = 0
        if job.current_epoch > 0:
            packages_completed = Package.select().where(
                Package.job == job,
                Package.epoch_num <= job.current_epoch,
                Package.status == PackageStatus.MERGED.value
            ).count()

        total_packages = job.num_epochs * job.num_packages
        progress = calculate_job_progress(job)

        return JobProgressResponse(
            job_id=job.job_id,
            status=APIJobStatus(job.status),
            current_epoch=job.current_epoch,
            total_epochs=job.num_epochs,
            packages_completed=packages_completed,
            total_packages=total_packages,
            progress_percentage=progress,
            force_algorithm=job.force_algorithm,
            barnes_hut_theta=job.barnes_hut_theta,
            integration_method=job.integration_method
        )

    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")


def _reject_v2(job_id: int, action: str) -> None:
    """The generic lifecycle routes change PostgreSQL only. A V2 job's
    execution state lives in Redis, so these routes would report success while
    its workers carried on. Send V2 jobs to /api/v2/jobs/{id}/{action}."""
    job = Job.get_or_none(Job.job_id == job_id)
    if job is not None and job.job_type == JobType.DISTRIBUTED_V2.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} is a distributed_v2 job, use POST /api/v2/jobs/{job_id}/{action}",
        )


@router.delete("/{job_id}")
def cancel_job(job_id: int, current_user: User = Depends(get_current_user)):
    """Cancel a running job."""
    try:
        _reject_v2(job_id, "cancel")
        # Atomic conditional transition: flip to CANCELLED only if not already
        # terminal. Avoids the read-check-modify-write race with concurrent
        # pause/resume/cancel that could lose an update.
        terminal = [JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value]
        updated = (Job.update(status=JobStatus.CANCELLED.value, updated_at=utcnow())
                   .where((Job.job_id == job_id) & (Job.status.not_in(terminal)))
                   .execute())
        if updated == 0:
            job = Job.get_or_none(Job.job_id == job_id)
            if job is None:
                raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Job {job_id} is already {job.status.lower()}"
            )

        logger.info(f"Cancelled job {job_id}")
        return {"message": f"Job {job_id} cancelled successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cancel job {job_id}: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to cancel job")


@router.post("/{job_id}/pause")
def pause_job(job_id: int, current_user: User = Depends(get_current_user)):
    """Pause a running job. The worker will stop after the current progress checkpoint."""
    try:
        _reject_v2(job_id, "pause")
        # Atomic conditional transition: only PENDING/IN_PROGRESS -> PAUSED.
        allowed = [JobStatus.IN_PROGRESS.value, JobStatus.PENDING.value]
        updated = (Job.update(status=JobStatus.PAUSED.value, updated_at=utcnow())
                   .where((Job.job_id == job_id) & (Job.status.in_(allowed)))
                   .execute())
        if updated == 0:
            job = Job.get_or_none(Job.job_id == job_id)
            if job is None:
                raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Can only pause PENDING or IN_PROGRESS jobs (current: {job.status})"
            )

        job = Job.get_by_id(job_id)
        logger.info(f"Paused job {job_id} at epoch {job.current_epoch}")
        return {"message": f"Job {job_id} paused at epoch {job.current_epoch}"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to pause job {job_id}: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to pause job")


@router.post("/{job_id}/resume")
async def resume_job(job_id: int, current_user: User = Depends(get_current_user)):
    """Resume a paused job. Republishes the message for monolithic jobs."""
    try:
        _reject_v2(job_id, "resume")
        # Atomic conditional transition: only PAUSED -> IN_PROGRESS. Ensures
        # exactly one concurrent resume wins, so the republish below happens once.
        updated = (Job.update(status=JobStatus.IN_PROGRESS.value,
                              worker_id=None, leased_at=None, updated_at=utcnow())
                   .where((Job.job_id == job_id) & (Job.status == JobStatus.PAUSED.value))
                   .execute())
        if updated == 0:
            job = Job.get_or_none(Job.job_id == job_id)
            if job is None:
                raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Can only resume PAUSED jobs (current: {job.status})"
            )

        job = Job.get_by_id(job_id)

        # Publish only after the status flip (one concurrent resume wins). If
        # publishing fails, put the job back to PAUSED so it can be resumed
        # again, instead of leaving it IN_PROGRESS with no message to run it.
        try:
            if job.job_type in [JobType.MONOLITHIC.value]:
                message = MonolithicJobMessage.from_job(job)
                target_queue = GPU_MONOLITHIC_QUEUE if job.gpu_enabled else MONOLITHIC_QUEUE
                await publish_persistent([(target_queue, message.to_json())])
                logger.info(f"Republished monolithic job {job_id} to {target_queue}")
            elif job.job_type == JobType.DISTRIBUTED.value:
                await on_rabbitmq_channel(lambda channel: queue_v1_epoch(job, channel))
        except Exception as publish_error:
            Job.update(status=JobStatus.PAUSED.value, updated_at=utcnow()).where(
                (Job.job_id == job_id) & (Job.status == JobStatus.IN_PROGRESS.value)
                & (Job.worker_id.is_null())
            ).execute()
            logger.error(f"Could not queue resumed job {job_id}, left PAUSED: {publish_error}")
            # 503 (retry later) when the broker is down. A refused message or
            # a database error is not fixed by retrying
            if isinstance(publish_error, BrokerUnavailable):
                raise HTTPException(status_code=503,
                                    detail="Could not queue the job, it is still paused, try again")
            raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=f"Could not queue the job, it is still paused: {publish_error}")

        logger.info(f"Resumed job {job_id} from epoch {job.current_epoch}")
        return {"message": f"Job {job_id} resumed from epoch {job.current_epoch}"}

    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to resume job {job_id}: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to resume job")


@router.delete("/{job_id}/permanent")
async def delete_job_permanently(
    job_id: int = Path(..., ge=1, le=2147483647, description="Job ID (must be positive integer)"),
    current_user: User = Depends(get_current_user)
):
    """Permanently delete a job and all its associated data.

    Only completed, failed, or cancelled jobs can be deleted.
    Deletes:
    - Job record from database
    - All package records
    - All result files from S3 (epochresults and partialresults buckets)

    Does NOT delete:
    - Input file (kept for potential reuse)
    """
    try:
        job = Job.get(Job.job_id == job_id)

        if job.job_type == JobType.DISTRIBUTED_V2.value:
            # A V2 job's state is in Redis and its objects under v2/job_id/.
            # The cleanup below knows neither, and deleting the row first
            # would leave the V2 endpoint nothing to work from. The dashboard
            # deletes every job through this route, so V2 jobs are handed over.
            return await asyncio.to_thread(closes_connection(delete_v2_job_permanent), job_id,
                                           current_user=current_user)

        if job.status not in [JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value]:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Cannot delete job in status '{job.status}'. Only completed, failed, or cancelled "
                       "jobs can be deleted."
            )

        # Delete database records first (fast — keeps UI responsive)
        deleted_packages_count = 0
        try:
            with db.atomic():
                deleted_packages_count = Package.delete().where(Package.job == job).execute()
                logger.info(f"Deleted {deleted_packages_count} packages for job {job_id}")

                job.delete_instance()
                logger.info(f"Deleted job {job_id} from database")
        except Exception as e:
            logger.error(f"Database transaction failed for job {job_id}: {e}")
            raise HTTPException(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete job {job_id} from database: {str(e)}"
            )

        # Clean up S3 files in background (can take minutes for large jobs)
        job_prefix = f"job_id/{job_id}/"

        async def _cleanup_s3():
            total = 0
            for bucket in [PARTIAL_RESULTS_BUCKET_NAME, EPOCH_RESULTS_BUCKET_NAME]:
                try:
                    count = await asyncio.to_thread(delete_files_by_prefix, bucket, job_prefix)
                    total += count
                    logger.info(f"Background S3 cleanup: deleted {count} files from {bucket} for job {job_id}")
                except Exception as e:
                    logger.error(f"Background S3 cleanup failed for {bucket}/job {job_id}: {e}")
            logger.info(f"Background S3 cleanup complete for job {job_id}: {total} files deleted")

        # keep a reference until it finishes: the event loop holds tasks only
        # weakly, so an unreferenced task can be garbage-collected mid-run
        task = asyncio.create_task(_cleanup_s3())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        return {
            "message": f"Job {job_id} deleted. S3 cleanup running in background.",
            "deleted_packages": deleted_packages_count,
            "deleted_files": 0
        }

    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete job {job_id}: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to delete job")


@router.post("/{job_id}/regenerate-message")
async def regenerate_job_message(
    job_id: int = Path(..., ge=1, le=2147483647, description="Job ID"),
    current_user: User = Depends(get_current_user)
):
    """Regenerate RabbitMQ message for an orphaned monolithic job.

    Use this endpoint when watchdog detects an orphaned job (IN_PROGRESS but no RabbitMQ message).
    """
    try:
        job = Job.get(Job.job_id == job_id)

        if job.job_type != JobType.MONOLITHIC.value:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Job {job_id} is not a monolithic job (type: {job.job_type}). Only monolithic jobs "
                       "can have messages regenerated."
            )

        if job.status != JobStatus.IN_PROGRESS.value:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Job {job_id} is not in IN_PROGRESS status (status: {job.status}). Only IN_PROGRESS "
                       "jobs can have messages regenerated."
            )

        logger.info(f"Regenerating message for orphaned job {job_id}")
        logger.info(f"Job details: epochs={job.num_epochs}, current_epoch={job.current_epoch}, "
                    f"gpu_enabled={job.gpu_enabled}")

        message = MonolithicJobMessage.from_job(job)

        target_queue = GPU_MONOLITHIC_QUEUE if job.gpu_enabled else MONOLITHIC_QUEUE
        try:
            await publish_persistent([(target_queue, message.to_json())])
            logger.info(f"Successfully regenerated and published message for job {job_id} to {target_queue} "
                        f"(GPU: {job.gpu_enabled})")
        except Exception as publish_error:
            logger.error(f"Failed to publish regenerated message for job {job_id}: {publish_error}")
            raise HTTPException(
                status_code=503 if isinstance(publish_error, BrokerUnavailable) else HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to publish message to queue: {str(publish_error)}"
            )

        return {
            "message": f"Successfully regenerated message for job {job_id}",
            "job_id": job.job_id,
            "status": job.status,
            "current_epoch": job.current_epoch,
            "total_epochs": job.num_epochs,
            "gpu_enabled": job.gpu_enabled,
            "queue": target_queue
        }

    except Job.DoesNotExist:
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to regenerate message for job {job_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to regenerate message: {str(e)}"
        )


@router.get("/{job_id}/packages", response_model=List[PackageResponse])
def list_job_packages(
    job_id: int,
    epoch_num: Optional[int] = Query(None, ge=0),
    limit: int = Query(1000, ge=1, le=10000, description="Packages per page"),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """List a job's packages (a page of them, in epoch and package order),
    optionally filtered by epoch."""
    try:
        job = Job.get(Job.job_id == job_id)

        query = Package.select().where(Package.job == job)

        if epoch_num is not None:
            query = query.where(Package.epoch_num == epoch_num)

        packages = (query.order_by(Package.epoch_num, Package.package_number, Package.package_id)
                    .limit(limit).offset(offset))

        return [
            PackageResponse(
                package_id=pkg.package_id,
                package_number=pkg.package_number,
                epoch_num=pkg.epoch_num,
                status=pkg.status,
                file_key=pkg.file_key
            )
            for pkg in packages
        ]

    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list packages for job {job_id}: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to list packages")
