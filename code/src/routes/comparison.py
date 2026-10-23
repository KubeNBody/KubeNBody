# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
API endpoints for computing conservation metrics for simulation jobs.
Metrics are computed per-epoch and can be used for visualization and analysis.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_500_INTERNAL_SERVER_ERROR

from api_schemas import (
    ComparisonCacheStatsResponse,
    ComparisonJobListResponse,
    ComparisonJobResponse,
    ComparisonJobSubmitRequest,
    EpochMetricsResponse,
    JobComparisonSummary,
    JobMetricsResponse,
    JobToJobComparisonRequest,
    JobToJobComparisonResponse,
)
from async_file_service import EPOCH_RESULTS_BUCKET_NAME
from auth import get_current_user
from constants import SOFTENING_SQ
from db_threads import ClosingRoute, closes_connection
from file_service import delete_file, download_file
from logging_config import setup_logging
from models import ComparisonJob, InputFile, Job, User
from publisher import publish_persistent
from rabbitmq import COMPARISON_QUEUE, BrokerUnavailable
from schemas import ComparisonMessage, JobStatus
from services.comparison_cache import (
    _metrics_result_to_dict,
    cache_metrics,
    delete_persistent_metrics,
    get_cache_stats,
    get_cached_metrics,
    get_persistent_metrics,
    invalidate_job_cache,
    store_persistent_metrics,
)
from services.comparison_service import (
    compute_full_comparison,
    compute_job_metrics_streaming,
)


logger = setup_logging('api.comparison')

router = APIRouter(prefix="/api/comparison", tags=["Comparison"], route_class=ClosingRoute)

# Single-flight locks for metrics computation, keyed by (job_id, max_snapshots).
# One per key seen in this process's lifetime, never evicted.
_metrics_locks: dict = {}


def _metrics_compute_lock(job_id: int, max_snapshots: Optional[int]) -> asyncio.Lock:
    key = (job_id, max_snapshots)
    lock = _metrics_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _metrics_locks[key] = lock
    return lock


def _job_to_summary(job: Job) -> JobComparisonSummary:
    """Convert Job model to comparison summary."""
    return JobComparisonSummary(
        job_id=job.job_id,
        status=job.status,
        job_type=job.job_type,
        num_epochs=job.num_epochs,
        current_epoch=job.current_epoch,
        snapshot_interval=job.snapshot_interval,
        force_algorithm=job.force_algorithm or 'direct',
        barnes_hut_theta=job.barnes_hut_theta,
        softening_sq=job.softening_sq if job.softening_sq is not None else SOFTENING_SQ,
        integration_method=job.integration_method or 'simple_euler',
        time_step=job.time_step,
        gpu_enabled=job.gpu_enabled if job.gpu_enabled is not None else False,
        filename=job.filename,
        description=job.description,
        name=job.name,
    )


def _metrics_to_response(metrics_dict: dict) -> JobMetricsResponse:
    """Convert cached metrics dict to response model."""
    return JobMetricsResponse(
        job_id=metrics_dict['job_id'],
        epochs=metrics_dict['epochs'],
        simulation_times=metrics_dict.get('simulation_times', []),
        metrics=[
            EpochMetricsResponse(
                epoch=m['epoch'],
                simulation_time=m.get('simulation_time', 0.0),
                kinetic_energy=m['kinetic_energy'],
                potential_energy=m['potential_energy'],
                total_energy=m['total_energy'],
                relative_energy_error=m['relative_energy_error'],
                momentum=tuple(m['momentum']),
                momentum_magnitude=m['momentum_magnitude'],
                angular_momentum=tuple(m['angular_momentum']),
                angular_momentum_magnitude=m['angular_momentum_magnitude']
            )
            for m in metrics_dict['metrics']
        ],
        energy_drift_percent=metrics_dict['energy_drift_percent'],
        momentum_conserved=metrics_dict['momentum_conserved'],
        angular_momentum_conserved=metrics_dict['angular_momentum_conserved'],
        computation_time_ms=metrics_dict['computation_time_ms'],
        simulation_times_exact=metrics_dict.get('simulation_times_exact', True),
        snapshots_missing=metrics_dict.get('snapshots_missing', 0),
    )


@router.get("/jobs/{job_id}/metrics", response_model=JobMetricsResponse)
async def get_job_metrics(
    job_id: int,
    max_snapshots: Optional[int] = Query(None, ge=1, description="Maximum snapshots to analyze (None = all snapshots)"),
    force_refresh: bool = Query(False, description="Force recomputation, bypass cache"),
    current_user: User = Depends(get_current_user)
):
    """
    Compute conservation metrics for a single job.

    Returns energy, momentum, and angular momentum for each snapshot epoch,
    along with drift analysis and conservation status.

    Metrics per epoch:
    - kinetic_energy: KE = 0.5 * sum(m * v^2)
    - potential_energy: PE = -G * sum(m_i * m_j / r_ij)
    - total_energy: KE + PE
    - relative_energy_error: (E - E0) / |E0|
    - momentum: (Px, Py, Pz) and magnitude
    - angular_momentum: (Lx, Ly, Lz) and magnitude

    Summary:
    - energy_drift_percent: Overall energy change from initial
    - momentum_conserved: True if momentum stays within 1% of initial
    - angular_momentum_conserved: True if L stays within 1% of initial
    """
    try:
        job = Job.get(Job.job_id == job_id)

        # Only analyze completed or in-progress jobs
        if job.status not in [JobStatus.COMPLETED.value, JobStatus.IN_PROGRESS.value]:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Job {job_id} cannot be analyzed (status: {job.status})"
            )

        # Check caches (only for completed jobs — their snapshots are
        # immutable. In-progress jobs might have new data).
        # L1: in-memory TTL. L2: S3-persisted, survives pod restarts.
        if not force_refresh and job.status == JobStatus.COMPLETED.value:
            cached = get_cached_metrics(job_id, max_snapshots)
            if cached:
                logger.info(f"Cache hit for job {job_id} metrics (max_snapshots={max_snapshots})")
                return _metrics_to_response(cached)
            cached = await get_persistent_metrics(job_id, max_snapshots)
            if cached:
                return _metrics_to_response(cached)

        # Single-flight: concurrent requests (e.g. frontend retries) for the
        # same job wait for one computation instead of piling up copies.
        async with _metrics_compute_lock(job_id, max_snapshots):
            if not force_refresh and job.status == JobStatus.COMPLETED.value:
                cached = get_cached_metrics(job_id, max_snapshots)
                if cached:
                    return _metrics_to_response(cached)

            logger.info(f"Computing metrics for job {job_id} (max_snapshots={max_snapshots})")
            result = await compute_job_metrics_streaming(job, max_snapshots=max_snapshots)

            # Cache result for completed jobs (memory + persistent)
            if job.status == JobStatus.COMPLETED.value:
                cache_metrics(job_id, max_snapshots, result)
                await store_persistent_metrics(job_id, max_snapshots, result)

        return _metrics_to_response(_metrics_result_to_dict(result))

    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to compute metrics for job {job_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to compute metrics: {str(e)}"
        )


@router.get("/jobs/{job_id}/summary", response_model=JobComparisonSummary)
def get_job_comparison_summary(
    job_id: int,
    current_user: User = Depends(get_current_user)
):
    """
    Get job summary for comparison UI.

    Returns job parameters useful for comparison:
    - Algorithm settings (force_algorithm, barnes_hut_theta)
    - Integration method
    - Time step
    - GPU enabled status
    """
    try:
        job = Job.get(Job.job_id == job_id)
        return _job_to_summary(job)
    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")


@router.get("/jobs/summaries", response_model=List[JobComparisonSummary])
def get_multiple_job_summaries(
    job_ids: str = Query(..., description="Comma-separated list of job IDs"),
    current_user: User = Depends(get_current_user)
):
    """
    Get summaries for multiple jobs at once.

    Useful for populating comparison UI with job parameters.
    """
    try:
        ids = [int(id.strip()) for id in job_ids.split(",") if id.strip()]
        ids = list(dict.fromkeys(ids))
    except ValueError:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="Invalid job_ids format. Use comma-separated integers."
        )

    if any(id <= 0 for id in ids):
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="All job IDs must be positive integers."
        )

    # a saved comparison's reference plus its up to 10 comparison jobs
    if len(ids) > 11:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="Maximum 11 jobs can be queried at once"
        )

    summaries = []
    for job_id in ids:
        try:
            job = Job.get(Job.job_id == job_id)
            summaries.append(_job_to_summary(job))
        except Job.DoesNotExist:
            logger.warning(f"Job {job_id} not found when fetching summaries")
            continue

    return summaries


@router.delete("/cache/{job_id}")
async def invalidate_comparison_cache(
    job_id: int,
    current_user: User = Depends(get_current_user)
):
    """
    Invalidate cached comparison data for a specific job.

    Useful when job data has changed and cached metrics are stale.
    """
    count = invalidate_job_cache(job_id)
    # the persistent copy too, or the next lookup restores it
    try:
        persisted = await asyncio.to_thread(delete_persistent_metrics, job_id)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Could not delete persisted metrics of job {job_id}: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"In-memory cache cleared, persisted metrics not deleted: {e}")
    return {"message": f"Invalidated {count} cache entries and {persisted} persisted metrics "
                       f"for job {job_id}"}


@router.get("/cache/stats", response_model=ComparisonCacheStatsResponse)
async def get_comparison_cache_stats(current_user: User = Depends(get_current_user)):
    """Get comparison cache statistics."""
    stats = get_cache_stats()
    return ComparisonCacheStatsResponse(**stats)


# Job-to-Job Comparison Endpoint

@router.post("/compare", response_model=JobToJobComparisonResponse)
async def compare_jobs(
    request: JobToJobComparisonRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Compare multiple jobs against a reference job.

    Computes trajectory divergence (position/velocity RMSE) and energy conservation
    comparison between the reference and each comparison job.

    Use cases:
    - Algorithm validation: Direct vs Barnes-Hut
    - Integrator comparison: Euler vs Verlet vs RK4
    - Timestep convergence: dt=0.001 vs dt=0.01
    - GPU vs CPU validation

    Returns per-epoch timeseries for visualization plus summary statistics.
    """
    try:
        try:
            ref_job = Job.get(Job.job_id == request.reference_job_id)
        except Job.DoesNotExist:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=f"Reference job {request.reference_job_id} not found"
            )

        if ref_job.status != JobStatus.COMPLETED.value:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Reference job {request.reference_job_id} is not completed (status: {ref_job.status})"
            )

        cmp_jobs = []
        for job_id in request.comparison_job_ids:
            try:
                job = Job.get(Job.job_id == job_id)
            except Job.DoesNotExist:
                raise HTTPException(
                    status_code=HTTP_404_NOT_FOUND,
                    detail=f"Comparison job {job_id} not found"
                )

            if job.status != JobStatus.COMPLETED.value:
                raise HTTPException(
                    status_code=HTTP_400_BAD_REQUEST,
                    detail=f"Comparison job {job_id} is not completed (status: {job.status})"
                )
            cmp_jobs.append(job)

        result_dict = await compute_full_comparison(
            ref_job, cmp_jobs, max_snapshots=request.max_snapshots
        )

        return JobToJobComparisonResponse(**result_dict)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to compare jobs: {e}", exc_info=True)
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to compare jobs: {str(e)}"
        )


# Persistent Comparison Jobs

# Version of the comparison method, stored with each saved comparison (as
# job_fingerprints["_version"]): a comparison computed under another version
# is stale and not reused. Bump it whenever comparison semantics change, as
# with services.comparison_cache._PERSISTENT_CACHE_VERSION (the cached
# per-job metrics). Usually both change together.
# 3: momentum drift against sum m|v|. max_snapshots takes every k-th
#    reference snapshot counted back from its last. Comparison jobs read at
#    the reference's times (interpolation flags). Missing adaptive time
#    records estimated from the recorded ones (comparison cache v5)
COMPARISON_ALGORITHM_VERSION = 3
_VERSION_KEY = "_version"
OLDER_METHOD_REASON = "computed with an older comparison method"

def _build_job_fingerprint(job: Job, num_bodies_by_file: Optional[dict] = None) -> dict:
    """Build a fingerprint of job settings for staleness detection.
    num_bodies_by_file: input file id -> body count, if already loaded."""
    num_bodies = None
    if job.input_file_id:
        if num_bodies_by_file is not None:
            num_bodies = num_bodies_by_file.get(job.input_file_id)
        else:
            try:
                input_file = InputFile.get(InputFile.file_id == job.input_file_id)
                num_bodies = input_file.num_bodies
            except InputFile.DoesNotExist:
                pass
    completed_at = job.completed_at
    return {
        "num_bodies": num_bodies,
        "algorithm": job.force_algorithm or 'direct',
        "integration_method": job.integration_method or 'simple_euler',
        "time_step": job.time_step,
        "num_epochs": job.num_epochs,
        "adaptive_enabled": job.adaptive_enabled,
        "softening_sq": job.softening_sq,
        "barnes_hut_theta": job.barnes_hut_theta,
        "snapshot_interval": job.snapshot_interval,
        "input_file_key": job.input_file_key,
        # a recovered job reruns and completes again: new snapshots, same settings
        "recovery_count": job.recovery_count,
        "completed_at": completed_at.isoformat() if completed_at else None,
    }


def _changed_setting(stored_fp: dict, current_fp: dict) -> Optional[str]:
    """The first fingerprint key whose value changed, else None. Only keys
    present in both are compared: fingerprints stored before a key was added
    lack it."""
    for key in stored_fp:
        if key in current_fp and str(stored_fp[key]) != str(current_fp[key]):
            return key
    return None


def _stored_fingerprints(comparison: ComparisonJob) -> Optional[dict]:
    """A saved comparison's fingerprints, or None if they are missing,
    unreadable, or from another COMPARISON_ALGORITHM_VERSION."""
    try:
        stored = json.loads(comparison.job_fingerprints) if comparison.job_fingerprints else None
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(stored, dict) or stored.get(_VERSION_KEY) != COMPARISON_ALGORITHM_VERSION:
        return None
    return stored


def _check_staleness(comparison: ComparisonJob, preloaded: Optional[tuple] = None) -> tuple:
    """Check if a comparison's source jobs have changed since it was computed.
    preloaded: (job id -> Job, input file id -> body count) for a page of
    comparisons (see _preload_for_staleness), instead of two queries per job.

    Returns (is_stale: bool, reason: str or None)
    """
    stored = _stored_fingerprints(comparison)
    if stored is None:
        return True, OLDER_METHOD_REASON

    all_job_ids = [comparison.reference_job_id] + json.loads(comparison.comparison_job_ids)
    for job_id in all_job_ids:
        stored_fp = stored.get(str(job_id))
        if not stored_fp:
            continue
        try:
            if preloaded is not None:
                job = preloaded[0].get(job_id)
                if job is None:
                    raise Job.DoesNotExist()
                current_fp = _build_job_fingerprint(job, preloaded[1])
            else:
                job = Job.get(Job.job_id == job_id)
                current_fp = _build_job_fingerprint(job)
            key = _changed_setting(stored_fp, current_fp)
            if key is not None:
                return True, f"Job {job_id} setting '{key}' changed"
        except Job.DoesNotExist:
            return True, f"Job {job_id} no longer exists"
    return False, None


def _preload_for_staleness(comparisons) -> tuple:
    """The jobs of a page of comparisons and their input files' body counts,
    in two queries (_check_staleness)."""
    ids = set()
    for comp in comparisons:
        ids.add(comp.reference_job_id)
        ids.update(json.loads(comp.comparison_job_ids))
    jobs = {job.job_id: job for job in Job.select().where(Job.job_id.in_(list(ids)))} if ids else {}
    file_ids = [job.input_file_id for job in jobs.values() if job.input_file_id]
    num_bodies = ({f.file_id: f.num_bodies for f in InputFile.select().where(InputFile.file_id.in_(file_ids))}
                  if file_ids else {})
    return jobs, num_bodies


def _comparison_to_response(comp: ComparisonJob, include_results: bool = False,
                            preloaded: Optional[tuple] = None) -> ComparisonJobResponse:
    """Convert ComparisonJob model to response."""
    stale, stale_reason = _check_staleness(comp, preloaded)

    resp = ComparisonJobResponse(
        comparison_id=comp.comparison_id,
        status=comp.status,
        reference_job_id=comp.reference_job_id,
        comparison_job_ids=json.loads(comp.comparison_job_ids),
        max_snapshots=comp.max_snapshots,
        created_at=comp.created_at,
        updated_at=comp.updated_at,
        started_at=comp.started_at,
        computation_time_ms=comp.computation_time_ms,
        jobs_compared=comp.jobs_compared,
        jobs_skipped=comp.jobs_skipped,
        error_message=comp.error_message,
        stale=stale,
        stale_reason=stale_reason,
    )

    # COMPLETED_EMPTY too: its results say why every job was skipped
    if include_results and comp.status in ('COMPLETED', 'COMPLETED_EMPTY') and comp.results_file_key:
        try:
            data = download_file(EPOCH_RESULTS_BUCKET_NAME, comp.results_file_key)
            # download_file returns a decoded str, not a stream
            resp.results = json.loads(data)
        except Exception as e:
            logger.error(f"Failed to load comparison results from S3: {e}")
            resp.error_message = f"Results stored but failed to load: {str(e)[:200]}"

    return resp


# A PENDING or COMPUTING comparison not updated for this long is presumed
# lost (its message or worker): an identical request starts a new one.
COMPARISON_IN_FLIGHT_S = 3600


def _reusable_comparison(request, cmp_ids_json: str, fingerprints: dict) -> Optional[ComparisonJob]:
    """The newest identical comparison that can be returned instead of a new
    one: COMPLETED, or still in flight (updated within COMPARISON_IN_FLIGHT_S),
    and computed from the jobs as they are now (not stale: a recovered job
    has a new recovery_count / completed_at) by the current comparison
    method (COMPARISON_ALGORITHM_VERSION)."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=COMPARISON_IN_FLIGHT_S)
    candidates = (ComparisonJob
                  .select()
                  .where(
                      ComparisonJob.reference_job_id == request.reference_job_id,
                      ComparisonJob.comparison_job_ids == cmp_ids_json,
                      ComparisonJob.max_snapshots == request.max_snapshots,
                      (ComparisonJob.status == 'COMPLETED')
                      | (ComparisonJob.status.in_(['PENDING', 'COMPUTING'])
                         & (ComparisonJob.updated_at >= cutoff)),
                  )
                  .order_by(ComparisonJob.created_at.desc()))
    for comparison in candidates:
        stored = _stored_fingerprints(comparison)
        if stored is None:
            continue
        if all(_changed_setting(stored.get(job_id) or {}, current) is None
               for job_id, current in fingerprints.items()):
            return comparison
    return None


@router.post("/saved", response_model=ComparisonJobResponse)
async def create_comparison_job(
    request: ComparisonJobSubmitRequest,
    current_user: User = Depends(get_current_user)
):
    """Create a persistent comparison job that runs in the background.

    Results are stored in S3 and can be retrieved later by comparison_id.
    """
    try:
        # Validate reference job
        try:
            ref_job = Job.get(Job.job_id == request.reference_job_id)
        except Job.DoesNotExist:
            raise HTTPException(status_code=HTTP_404_NOT_FOUND,
                                detail=f"Reference job {request.reference_job_id} not found")
        if ref_job.status != JobStatus.COMPLETED.value:
            raise HTTPException(status_code=HTTP_400_BAD_REQUEST,
                                detail=f"Reference job must be COMPLETED (status: {ref_job.status})")

        fingerprints = {str(ref_job.job_id): _build_job_fingerprint(ref_job)}
        for job_id in request.comparison_job_ids:
            try:
                job = Job.get(Job.job_id == job_id)
            except Job.DoesNotExist:
                raise HTTPException(status_code=HTTP_404_NOT_FOUND,
                                    detail=f"Comparison job {job_id} not found")
            if job.status != JobStatus.COMPLETED.value:
                raise HTTPException(status_code=HTTP_400_BAD_REQUEST,
                                    detail=f"Job {job_id} must be COMPLETED (status: {job.status})")
            fingerprints[str(job_id)] = _build_job_fingerprint(job)

        # Deduplication: check for existing identical comparison
        cmp_ids_json = json.dumps(sorted(request.comparison_job_ids))
        existing = _reusable_comparison(request, cmp_ids_json, fingerprints)
        if existing:
            logger.info(f"Returning existing comparison {existing.comparison_id}")
            return _comparison_to_response(existing)

        comparison = ComparisonJob.create(
            reference_job_id=request.reference_job_id,
            comparison_job_ids=cmp_ids_json,
            max_snapshots=request.max_snapshots,
            status='PENDING',
            job_fingerprints=json.dumps({**fingerprints, _VERSION_KEY: COMPARISON_ALGORITHM_VERSION}),
        )

        msg = ComparisonMessage(
            comparison_id=comparison.comparison_id,
            reference_job_id=request.reference_job_id,
            comparison_job_ids=cmp_ids_json,
            max_snapshots=request.max_snapshots,
        )
        try:
            await publish_persistent([(COMPARISON_QUEUE, msg.to_json())])
        except Exception as publish_error:
            # A PENDING row that no message will ever run would also be what
            # the deduplication above returns for every identical request.
            comparison.delete_instance()
            logger.error(f"Could not queue comparison {comparison.comparison_id}, "
                         f"row removed: {publish_error}")
            if isinstance(publish_error, BrokerUnavailable):
                raise HTTPException(status_code=503, detail="Could not queue the comparison, try again")
            raise HTTPException(status_code=500,
                                detail=f"Could not queue the comparison: {publish_error}")

        logger.info(f"Created comparison job {comparison.comparison_id}: "
                     f"ref={request.reference_job_id}, cmp={request.comparison_job_ids}")

        return _comparison_to_response(comparison)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create comparison job: {e}", exc_info=True)
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Failed to create comparison: {str(e)}")


@router.get("/saved/{comparison_id}", response_model=ComparisonJobResponse)
async def get_comparison_job(
    comparison_id: int,
    current_user: User = Depends(get_current_user)
):
    """Get a persistent comparison job by ID.

    Returns full results if the comparison is COMPLETED.
    """
    try:
        comparison = ComparisonJob.get(ComparisonJob.comparison_id == comparison_id)
    except ComparisonJob.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND,
                            detail=f"Comparison {comparison_id} not found")
    # include_results=True does a blocking S3 GET. Run the whole (sync) builder in
    # a thread so it doesn't stall the event loop.
    return await asyncio.to_thread(closes_connection(_comparison_to_response), comparison, include_results=True)


@router.get("/saved", response_model=ComparisonJobListResponse)
def list_comparison_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str = Query(None, description="Filter by status"),
    current_user: User = Depends(get_current_user)
):
    """List all persistent comparison jobs, paginated."""
    query = ComparisonJob.select().order_by(ComparisonJob.created_at.desc())
    if status:
        query = query.where(ComparisonJob.status == status)
    total = query.count()
    comparisons = list(query.paginate(page, page_size))
    pages = (total + page_size - 1) // page_size
    preloaded = _preload_for_staleness(comparisons)

    return ComparisonJobListResponse(
        comparisons=[_comparison_to_response(c, preloaded=preloaded) for c in comparisons],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.delete("/saved/{comparison_id}")
def delete_comparison_job(
    comparison_id: int,
    current_user: User = Depends(get_current_user)
):
    """Delete a persistent comparison job and its S3 results."""
    try:
        comparison = ComparisonJob.get(ComparisonJob.comparison_id == comparison_id)
    except ComparisonJob.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND,
                            detail=f"Comparison {comparison_id} not found")

    # Delete S3 results if they exist. Delete ONLY this comparison's exact
    # results object (results_file_key), NOT a job_id/{id}/ prefix: comparison
    # IDs and simulation job IDs are independent sequences that share the
    # job_id/{id}/ namespace, so a prefix delete here would wipe an unrelated
    # simulation job's results (positions, snapshots, animation).
    if comparison.results_file_key:
        try:
            delete_file(EPOCH_RESULTS_BUCKET_NAME, comparison.results_file_key)
        except Exception as e:
            logger.warning(f"Failed to delete S3 results for comparison {comparison_id}: {e}")

    comparison.delete_instance()
    logger.info(f"Deleted comparison {comparison_id}")
    return {"deleted": True, "comparison_id": comparison_id}
