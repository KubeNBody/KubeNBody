# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Endpoints for downloading results and retrieving animation data.
"""

import asyncio
import csv
import gzip
import io
import os
from collections import deque
from typing import Dict, List

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from animation_cache import (
    cache_full_animation,
    cache_snapshots,
    effective_page_limit,
    get_cached_full_animation,
    get_cached_snapshots,
)
from api_schemas import AnimationSnapshot, PaginatedAnimationDataResponse, SnapshotInfo, SnapshotsListResponse
from async_file_service import EPOCH_RESULTS_BUCKET_NAME, AsyncS3Service
from auth import get_current_user
from db_threads import ClosingRoute
from file_service import generate_animation_file_from_path_async, get_s3, load_animation_data_async
from logging_config import setup_logging
from models import Job, User
from schemas import JobStatus, JobType
from transient_errors import is_missing_object, is_retryable_s3_error
from utils.job_helpers import is_combined_results_file, snapshot_epochs
from v2.binary_format import binary_to_numpy
from v2.file_service_v2 import download_snapshot as download_v2_snapshot
from v2.file_service_v2 import list_snapshots as list_v2_snapshots


logger = setup_logging('api.results')

router = APIRouter(prefix="/api/jobs", tags=["Jobs"], route_class=ClosingRoute)

# Up to this many epochs are checked with one HEAD each (an animation page).
# More are found by listing the job's prefix, 1000 keys per request.
MAX_SNAPSHOT_HEADS = 32


def _snapshot_exists(bucket: str, file_key: str) -> bool:
    """Check if a snapshot file exists in S3 (blocking). Only a missing
    object answers False: an outage raises, like the listing path, rather
    than drop epochs from a page that is then cached."""
    try:
        get_s3().meta.client.head_object(Bucket=bucket, Key=file_key)
        return True
    except Exception as e:
        if is_missing_object(e):
            logger.debug(f'Snapshot file not found: {file_key} - {e}')
            return False
        raise


def _listed_snapshot_epochs(job_id: int) -> set:
    """Epochs with an epoch_results.csv under the job's prefix (blocking)."""
    prefix = f'job_id/{job_id}/epoch_num/'
    suffix = '/epoch_results.csv'
    epochs = set()
    paginator = get_s3().meta.client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=EPOCH_RESULTS_BUCKET_NAME, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith(suffix):
                try:
                    epochs.add(int(key[len(prefix):-len(suffix)]))
                except ValueError:
                    continue
    return epochs


def _existing_snapshot_epochs(snapshot_epochs: List[int], job_id: int) -> List[int]:
    if len(snapshot_epochs) <= MAX_SNAPSHOT_HEADS:
        return [e for e in snapshot_epochs if _snapshot_exists(
            EPOCH_RESULTS_BUCKET_NAME, f'job_id/{job_id}/epoch_num/{e}/epoch_results.csv')]
    listed = _listed_snapshot_epochs(job_id)
    return [e for e in snapshot_epochs if e in listed]


async def verify_snapshots_exist(snapshot_epochs: List[int], job_id: int) -> List[int]:
    """The snapshot epochs whose files exist in S3, in order. One worker
    thread and a bounded number of requests whatever the job's length: one
    HEAD per epoch for a page, else a listing of the job's prefix (a HEAD per
    epoch of a whole job would queue tens of thousands on the default
    executor and starve /api/ready)."""
    try:
        found = await asyncio.to_thread(_existing_snapshot_epochs, list(snapshot_epochs), job_id)
    except Exception as e:
        if is_retryable_s3_error(e):
            logger.warning(f'Snapshot check for job {job_id} failed: {e}')
            raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE,
                                detail="Object storage is unavailable, try again shortly")
        raise
    missing = len(snapshot_epochs) - len(found)
    if missing:
        present = set(found)
        sample = [e for e in snapshot_epochs if e not in present][:10]
        logger.warning(f'{missing} snapshot file(s) missing for job {job_id}, e.g. epochs {sample}')
    return found


def _snapshot_from_csv(raw_file: str, epoch_num: int) -> dict:
    """Parse one snapshot CSV into the animation format (CPU work: run off
    the event loop)."""
    csv_reader = csv.DictReader(io.StringIO(raw_file))

    particles = []
    for row in csv_reader:
        try:
            particles.append([
                float(row.get('x', 0)),
                float(row.get('y', 0)),
                float(row.get('z', 0)),
                float(row.get('m', 1))
            ])
        except (ValueError, TypeError, KeyError) as e:
            logger.warning(f'Skipping malformed row in epoch {epoch_num}: {e}')
            continue

    if not particles:
        raise ValueError(f'No valid particles found in snapshot for epoch {epoch_num}')

    total_mass = 0.0
    com_x, com_y, com_z = 0.0, 0.0, 0.0

    for p in particles:
        mass = p[3]
        total_mass += mass
        com_x += p[0] * mass
        com_y += p[1] * mass
        com_z += p[2] * mass

    if total_mass > 0:
        com_x /= total_mass
        com_y /= total_mass
        com_z /= total_mass
    else:
        n = len(particles)
        if n > 0:
            com_x = sum(p[0] for p in particles) / n
            com_y = sum(p[1] for p in particles) / n
            com_z = sum(p[2] for p in particles) / n

    return {
        "epoch": epoch_num,
        "particles": particles,
        "center_of_mass": (com_x, com_y, com_z)
    }


async def download_snapshot_async(service: AsyncS3Service, file_key: str, epoch_num: int) -> dict:
    """Download and parse a single snapshot asynchronously."""
    try:
        raw_file = await service.download_file(EPOCH_RESULTS_BUCKET_NAME, file_key)
        return await asyncio.to_thread(_snapshot_from_csv, raw_file, epoch_num)
    except Exception as e:
        logger.error(f'Failed to download snapshot for epoch {epoch_num}: {e}')
        raise


async def download_snapshots_async(snapshot_epochs: List[int], job_id: int) -> List[dict]:
    """Download multiple snapshots concurrently using asyncio."""
    file_keys = [
        f'job_id/{job_id}/epoch_num/{epoch}/epoch_results.csv'
        for epoch in snapshot_epochs
    ]

    async with AsyncS3Service() as service:         # one client for the batch
        return await asyncio.gather(*[
            download_snapshot_async(service, file_key, epoch)
            for file_key, epoch in zip(file_keys, snapshot_epochs)
        ])


def _iter_s3_lines(key: str, chunk_size: int = 1 << 16):
    """The lines of an S3 object, decoded, read as a stream (blocking: run in a
    thread). A combined CSV can be hundreds of MB: never held whole."""
    body = get_s3().Object(EPOCH_RESULTS_BUCKET_NAME, key).get()['Body']
    try:
        for line in body.iter_lines(chunk_size=chunk_size):
            yield line.decode('utf-8')
    finally:
        body.close()


def _combined_page_from_rows(lines, page_epochs: List[int], limit: int, job_id: int) -> tuple[List[dict], int]:
    """The snapshots of page_epochs in a combined CSV (an iterable of lines), in
    the animation format, and the effective page limit: page_epochs is cut to
    effective_page_limit for the job's N, known once the file's first epoch
    ends, so no more than the page's particle budget is ever held (CPU work:
    run off the event loop)."""
    csv_reader = csv.DictReader(lines)

    target_epochs = set(page_epochs)
    epochs_data: Dict[int, List] = {}
    first_epoch, first_count, n_known = None, 0, False

    for row in csv_reader:
        try:
            epoch = int(row.get('epoch', 0))

            if not n_known:
                if first_epoch is None:
                    first_epoch = epoch
                if epoch == first_epoch:
                    first_count += 1
                else:
                    # every epoch has N rows: the page can be cut now
                    n_known = True
                    limit = effective_page_limit(limit, first_count)
                    target_epochs = set(page_epochs[:limit])
                    for dropped in [e for e in epochs_data if e not in target_epochs]:
                        del epochs_data[dropped]

            if epoch not in target_epochs:
                continue

            id_body = int(row.get('idBody', 0))
            particle = (
                id_body,
                float(row.get('x', 0)),
                float(row.get('y', 0)),
                float(row.get('z', 0)),
                float(row.get('m', 1))
            )

            if epoch not in epochs_data:
                epochs_data[epoch] = []
            epochs_data[epoch].append(particle)

        except (ValueError, TypeError, KeyError) as e:
            logger.warning(f'Skipping malformed row in combined file for job {job_id}: {e}')
            continue

    if not n_known:             # a single epoch in the file
        limit = effective_page_limit(limit, first_count)

    snapshots = []
    for epoch in sorted(epochs_data.keys()):
        particles_with_ids = epochs_data[epoch]
        particles_with_ids.sort(key=lambda p: p[0])
        particles = [[p[1], p[2], p[3], p[4]] for p in particles_with_ids]

        total_mass = 0.0
        com_x, com_y, com_z = 0.0, 0.0, 0.0

        for p in particles:
            mass = p[3]
            total_mass += mass
            com_x += p[0] * mass
            com_y += p[1] * mass
            com_z += p[2] * mass

        if total_mass > 0:
            com_x /= total_mass
            com_y /= total_mass
            com_z /= total_mass

        snapshots.append({
            "epoch": epoch,
            "particles": particles,
            "center_of_mass": (com_x, com_y, com_z)
        })

    return snapshots, limit


async def parse_combined_file_paginated(
    job_id: int,
    results_file_key: str,
    snapshot_interval: int,
    num_epochs: int,
    offset: int,
    limit: int
) -> tuple[List[dict], int, int]:
    """Parse combined CSV file but only keep epochs needed for current page.
    Returns (snapshots, total snapshots, effective page limit). The file is
    streamed from S3, not downloaded whole."""
    all_snapshot_epochs = snapshot_epochs(num_epochs, snapshot_interval)
    total_snapshots = len(all_snapshot_epochs)

    page_epochs = all_snapshot_epochs[offset:offset + limit]

    if not page_epochs:
        return [], total_snapshots, limit

    logger.info(f"Parsing combined CSV for job {job_id}, extracting epochs: {page_epochs}")

    snapshots, limit = await asyncio.to_thread(
        lambda: _combined_page_from_rows(_iter_s3_lines(results_file_key), page_epochs, limit, job_id))

    logger.info(f"Parsed {len(snapshots)} snapshots from combined CSV for job {job_id}")
    return snapshots, total_snapshots, limit


async def _gzip_s3_response(key: str, filename: str) -> StreamingResponse:
    """Stream an S3 object gzip-compressed (~10x smaller for CSV). 404 if
    the object is gone (e.g. the object store was emptied)."""
    try:
        s3_obj = await asyncio.to_thread(lambda: get_s3().Object(EPOCH_RESULTS_BUCKET_NAME, key).get())
    except Exception as e:
        if is_missing_object(e):
            raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="The results file is missing from storage")
        raise

    def iter_s3_gzip():
        buf = io.BytesIO()
        gz = gzip.GzipFile(fileobj=buf, mode='wb')
        for chunk in s3_obj['Body'].iter_chunks(chunk_size=65536):
            gz.write(chunk)
            if buf.tell() > 32768:
                gz.flush()
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        gz.close()
        yield buf.getvalue()

    return StreamingResponse(iter_s3_gzip(), media_type='application/gzip',
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


# Per-epoch snapshot files downloaded ahead of the one being sent
SNAPSHOT_PREFETCH = 4


def _snapshot_csv_part(content: str, with_header: bool) -> bytes:
    """One snapshot CSV as a part of the concatenation: stripped, non-empty
    lines, newline-terminated, its header only for the first part."""
    lines = [line.strip() for line in content.strip().split('\n') if line.strip()]
    if not with_header:
        lines = lines[1:]
    return ('\n'.join(lines) + '\n').encode('utf-8') if lines else b''


async def _iter_concatenated_snapshots(job_id: int, epochs: List[int]):
    """The per-epoch snapshot CSVs of `epochs` as one CSV (one header), part by
    part: at most SNAPSHOT_PREFETCH files are held at a time, in epoch order."""
    keys = iter(f'job_id/{job_id}/epoch_num/{epoch}/epoch_results.csv' for epoch in epochs)
    async with AsyncS3Service() as service:
        pending = deque()

        def fetch_next():
            key = next(keys, None)
            if key is not None:
                pending.append(asyncio.ensure_future(service.download_file(EPOCH_RESULTS_BUCKET_NAME, key)))
        try:
            for _ in range(SNAPSHOT_PREFETCH):
                fetch_next()
            header_sent = False
            while pending:
                content = await pending.popleft()
                fetch_next()
                part = await asyncio.to_thread(_snapshot_csv_part, content, not header_sent)
                del content
                if part:
                    header_sent = True
                    yield part
        finally:
            for task in pending:
                task.cancel()


async def _concatenated_snapshots_response(job_id: int, epochs: List[int], filename: str) -> StreamingResponse:
    """Stream the concatenation of the snapshots of `epochs`. 404 (before
    anything is sent) if they are all empty."""
    parts = _iter_concatenated_snapshots(job_id, epochs)
    try:
        first = await parts.__anext__()
    except StopAsyncIteration:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND,
                            detail=f"All snapshot files are empty for job {job_id}")

    async def body():
        try:
            yield first
            async for part in parts:
                yield part
        finally:
            await parts.aclose()

    return StreamingResponse(body(), media_type='text/csv',
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.get("/{job_id}/results")
async def download_results(job_id: int, current_user: User = Depends(get_current_user)):
    """Download final results for a completed job."""
    try:
        job = Job.get(Job.job_id == job_id)

        if job.status != JobStatus.COMPLETED.value:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Job {job_id} is not completed (status: {job.status})"
            )

        if not job.results_file_key:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=f"Results not available for job {job_id}"
            )

        if is_combined_results_file(job.results_file_key):
            return await _gzip_s3_response(job.results_file_key, f"job-{job_id}-results.csv.gz")
        else:
            logger.warning(
                f"Job {job_id} marked COMPLETED but results_file_key ({job.results_file_key}) "
                f"is not a combined file. Building from individual snapshots on-demand."
            )

            verified_epochs = await verify_snapshots_exist(
                snapshot_epochs(job.num_epochs, job.snapshot_interval), job_id)
            if not verified_epochs:
                raise HTTPException(
                    status_code=HTTP_404_NOT_FOUND,
                    detail=f"No snapshot files found for job {job_id}"
                )
            return await _concatenated_snapshots_response(job_id, verified_epochs,
                                                          f"job-{job_id}-results.csv")

    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download results for job {job_id}: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to download results")


@router.get("/{job_id}/results/full-history")
async def download_full_history(job_id: int, current_user: User = Depends(get_current_user)):
    """Download combined results for all snapshot epochs (filtered by snapshot_interval)."""
    try:
        job = Job.get(Job.job_id == job_id)

        if job.status == JobStatus.COMPLETED.value and is_combined_results_file(job.results_file_key):
            logger.info(f"Job {job_id} has combined results file, streaming gzipped from S3")
            return await _gzip_s3_response(job.results_file_key, f"job-{job_id}-full-history.csv.gz")

        logger.info(f"Job {job_id} does not have combined file, building full history from individual snapshots")
        epochs = snapshot_epochs(job.current_epoch, job.snapshot_interval)

        if len(epochs) < 1:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Job {job_id} has no snapshots available yet (current epoch: {job.current_epoch})"
            )

        logger.info(f"Downloading {len(epochs)} snapshots for job {job_id} full history")

        verified_epochs = await verify_snapshots_exist(epochs, job_id)

        if len(verified_epochs) < 1:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=f"No snapshot files found for job {job_id}"
            )

        missing_count = len(epochs) - len(verified_epochs)
        if missing_count > 0:
            logger.warning(
                f"Job {job_id}: {missing_count} of {len(epochs)} snapshots are missing. "
                f"Returning {len(verified_epochs)} available snapshots."
            )

        return await _concatenated_snapshots_response(job_id, verified_epochs,
                                                      f"job-{job_id}-full-history.csv")

    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download full history for job {job_id}: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to download full history")


async def get_v2_animation_data_paginated(
    job: Job,
    offset: int,
    limit: int
) -> PaginatedAnimationDataResponse:
    """Handle animation data for V2 jobs with binary snapshots."""

    available_epochs = await asyncio.to_thread(list_v2_snapshots, job.job_id)   # S3 listing

    if len(available_epochs) < 2:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Need at least 2 snapshots for animation, found {len(available_epochs)}"
        )

    total_snapshots = len(available_epochs)
    paginated_epochs = available_epochs[offset:offset + limit]

    if offset > 0 and len(paginated_epochs) == 0:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Invalid offset {offset}, only {total_snapshots} snapshots available"
        )

    if job.status == JobStatus.COMPLETED.value:
        cached = get_cached_snapshots(job.job_id, offset, limit)
        if cached:
            logger.debug(f"Cache hit for V2 job {job.job_id} animation (offset={offset})")
            page_limit = effective_page_limit(limit, len(cached[0].particles))
            return PaginatedAnimationDataResponse(
                job_id=job.job_id,
                snapshot_interval=job.snapshot_interval,
                total_epochs=job.num_epochs,
                total_snapshots=total_snapshots,
                offset=offset,
                limit=page_limit,
                has_next=(offset + page_limit) < total_snapshots,
                snapshots=cached
            )

    def load_single_snapshot(epoch: int) -> AnimationSnapshot:
        binary_data = download_v2_snapshot(job.job_id, epoch)
        positions, velocities, masses, ids = binary_to_numpy(binary_data)

        particles = [
            [float(positions[i, 0]), float(positions[i, 1]), float(positions[i, 2]), float(masses[i])]
            for i in range(len(masses))
        ]

        total_mass = float(np.sum(masses))
        if total_mass > 0:
            com = np.average(positions, axis=0, weights=masses)
        else:
            com = np.mean(positions, axis=0)

        return AnimationSnapshot(
            epoch=epoch,
            particles=particles,
            center_of_mass=(float(com[0]), float(com[1]), float(com[2]))
        )

    snapshots = []
    failed_epochs = []

    async def load_snapshot_async(epoch: int):
        try:
            return await asyncio.to_thread(load_single_snapshot, epoch)
        except Exception as e:
            logger.warning(f"Failed to load V2 snapshot epoch {epoch} for job {job.job_id}: {e}")
            return None

    # The page is cut to the particle budget, for the N of its first readable
    # snapshot (loaded before the others, which are then loaded together)
    results = []
    while len(results) < len(paginated_epochs) and (not results or results[-1] is None):
        results.append(await load_snapshot_async(paginated_epochs[len(results)]))
    page_limit = limit
    if results and results[-1] is not None:
        page_limit = max(len(results), effective_page_limit(limit, len(results[-1].particles)))
        paginated_epochs = paginated_epochs[:page_limit]
    results += await asyncio.gather(*[load_snapshot_async(epoch) for epoch in paginated_epochs[len(results):]],
                                    return_exceptions=True)
    has_next = (offset + page_limit) < total_snapshots

    for i, result in enumerate(results):
        epoch = paginated_epochs[i]
        if isinstance(result, Exception):
            logger.error(f"Exception loading snapshot {epoch}: {result}")
            failed_epochs.append(epoch)
        elif result is None:
            failed_epochs.append(epoch)
        else:
            snapshots.append(result)

    if len(failed_epochs) > 0:
        logger.warning(f"Failed to load {len(failed_epochs)} snapshots for job {job.job_id}: {failed_epochs}")
        if len(snapshots) < len(paginated_epochs) // 2:
            raise HTTPException(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to load {len(failed_epochs)} of {len(paginated_epochs)} snapshots"
            )

    snapshots.sort(key=lambda s: s.epoch)

    if job.status == JobStatus.COMPLETED.value and len(snapshots) > 0:
        cache_snapshots(job.job_id, offset, limit, snapshots)

    logger.info(f"Loaded {len(snapshots)} V2 snapshots for job {job.job_id} (offset={offset}, limit={page_limit})")

    return PaginatedAnimationDataResponse(
        job_id=job.job_id,
        snapshot_interval=job.snapshot_interval,
        total_epochs=job.num_epochs,
        total_snapshots=total_snapshots,
        offset=offset,
        limit=page_limit,
        has_next=has_next,
        snapshots=snapshots
    )


@router.get("/{job_id}/animation-data/paginated", response_model=PaginatedAnimationDataResponse)
async def get_animation_data_paginated(
    job_id: int,
    offset: int = Query(0, ge=0, description="Snapshot index to start from"),
    limit: int = Query(20, ge=1, le=100, description="Number of snapshots to return"),
    current_user: User = Depends(get_current_user)
):
    """Get paginated animation data for a job's snapshots.

    A page carries at most PAGE_PARTICLE_BUDGET particles (snapshots x N):
    `limit` is cut to that for large N, and the response's `limit` is the
    effective one (has_next follows it). The dashboard uses the same budget,
    so it never asks for more than this returns.
    """

    try:
        job = Job.get(Job.job_id == job_id)

        # V2 jobs use binary snapshots
        if job.job_type == JobType.DISTRIBUTED_V2.value:
            return await get_v2_animation_data_paginated(job, offset, limit)

        # For completed jobs, try to use pre-generated animation file
        if job.status == JobStatus.COMPLETED.value:
            animation_data = get_cached_full_animation(job_id)

            if animation_data is None:
                animation_data = await load_animation_data_async(job_id)

                if animation_data is None:
                    logger.info(f"Animation file not found for completed job {job_id}, generating lazily...")

                    if is_combined_results_file(job.results_file_key):
                        # Stream the CSV to disk and generate the animation
                        # JSON incrementally, never holding the whole file (or
                        # the whole animation) in memory: large jobs would
                        # otherwise run the pod out of memory.
                        csv_path = None
                        try:
                            service = AsyncS3Service()
                            csv_path = await service.stream_to_tempfile(EPOCH_RESULTS_BUCKET_NAME, job.results_file_key)
                            await generate_animation_file_from_path_async(job_id, csv_path)
                            animation_data = await load_animation_data_async(job_id)
                        except ValueError as e:
                            logger.warning(f"Animation generation skipped for job {job_id}: {e}")
                            animation_data = None
                        except Exception as e:
                            logger.error(f"Failed to generate animation for job {job_id}: {e}")
                            animation_data = None
                        finally:
                            if csv_path:
                                try:
                                    os.unlink(csv_path)
                                except OSError:
                                    pass
                    else:
                        animation_data = None

                if animation_data is not None:
                    cached = cache_full_animation(job_id, animation_data)
                    if cached:
                        logger.info(f"Cached full animation data for job {job_id}")
            else:
                logger.debug(f"Using cached full animation data for job {job_id}")

            if animation_data is not None:
                all_snapshots = animation_data.get("snapshots", [])
                total_snapshots = len(all_snapshots)

                if all_snapshots:
                    limit = effective_page_limit(limit, len(all_snapshots[0].get("particles", ())))
                snapshots_data = all_snapshots[offset:offset + limit]

                if offset > 0 and len(snapshots_data) == 0:
                    raise HTTPException(
                        status_code=HTTP_400_BAD_REQUEST,
                        detail=f"Offset {offset} exceeds available snapshots ({total_snapshots})"
                    )

                snapshots = [
                    AnimationSnapshot(epoch=s["epoch"], particles=s["particles"], center_of_mass=s["center_of_mass"])
                    for s in snapshots_data
                ]

                has_next = (offset + len(snapshots)) < total_snapshots

                return PaginatedAnimationDataResponse(
                    job_id=job_id,
                    snapshot_interval=job.snapshot_interval,
                    total_epochs=job.num_epochs,
                    total_snapshots=total_snapshots,
                    offset=offset,
                    limit=limit,
                    has_next=has_next,
                    snapshots=snapshots
                )

        # For in-progress jobs OR fallback. Only a completed job's pages are
        # cached: a running job's snapshot count grows (a cached page also
        # reported the final count, so has_next pointed past the last page)
        completed = job.status == JobStatus.COMPLETED.value
        requested_limit = limit         # the page cache's key. `limit` becomes the effective one
        cached_data = get_cached_snapshots(job_id, offset, limit) if completed else None
        if cached_data:
            limit = effective_page_limit(limit, len(cached_data[0]["particles"]))
            logger.info(f"Cache hit for job {job_id}, offset={offset}, limit={limit}")

            total_snapshots = len(snapshot_epochs(job.num_epochs, job.snapshot_interval))

            snapshots_data = cached_data
        else:
            logger.info(f"Cache miss for job {job_id}, offset={offset}, limit={limit}")

            if job.status == JobStatus.COMPLETED.value and is_combined_results_file(job.results_file_key):
                logger.info(f"Using paginated CSV parser for completed job {job_id} (fallback path)")
                snapshots_data, total_snapshots, limit = await parse_combined_file_paginated(
                    job_id=job_id,
                    results_file_key=job.results_file_key,
                    snapshot_interval=job.snapshot_interval,
                    num_epochs=job.num_epochs,
                    offset=offset,
                    limit=limit
                )

                if not snapshots_data and offset > 0:
                    raise HTTPException(
                        status_code=HTTP_400_BAD_REQUEST,
                        detail=f"Offset {offset} exceeds available snapshots ({total_snapshots})"
                    )

                cache_snapshots(job_id, offset, requested_limit, snapshots_data)
            else:
                expected_snapshot_epochs = snapshot_epochs(
                    job.current_epoch, job.snapshot_interval,
                    include_last=job.status == JobStatus.COMPLETED.value)

                total_snapshots = len(expected_snapshot_epochs)
                paginated_epochs = expected_snapshot_epochs[offset:offset + limit]

                if len(paginated_epochs) == 0:
                    raise HTTPException(
                        status_code=HTTP_400_BAD_REQUEST,
                        detail=f"Offset {offset} exceeds available snapshots ({total_snapshots})"
                    )

                verified_epochs = await verify_snapshots_exist(paginated_epochs, job_id)

                if len(verified_epochs) == 0:
                    raise HTTPException(
                        status_code=HTTP_400_BAD_REQUEST,
                        detail="No valid snapshots found in requested range"
                    )

                # Cut to the particle budget for the N of the first snapshot
                snapshots_data = await download_snapshots_async(verified_epochs[:1], job_id)
                limit = max(effective_page_limit(limit, len(snapshots_data[0]["particles"])),
                            paginated_epochs.index(verified_epochs[0]) + 1)
                rest = [e for e in verified_epochs[1:] if e in paginated_epochs[:limit]]
                if rest:
                    snapshots_data += await download_snapshots_async(rest, job_id)

                if completed:
                    cache_snapshots(job_id, offset, requested_limit, snapshots_data)

        snapshots = [
            AnimationSnapshot(epoch=s["epoch"], particles=s["particles"], center_of_mass=s["center_of_mass"])
            for s in snapshots_data
        ]

        has_next = (offset + len(snapshots)) < total_snapshots

        return PaginatedAnimationDataResponse(
            job_id=job_id,
            snapshot_interval=job.snapshot_interval,
            total_epochs=job.num_epochs,
            total_snapshots=total_snapshots,
            offset=offset,
            limit=limit,
            has_next=has_next,
            snapshots=snapshots
        )

    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get paginated animation data for job {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get animation data")


def _epochs_in_csv(lines) -> List[int]:
    """The epochs present in a combined CSV, an iterable of lines (the
    combination skips snapshots that were never written, so they cannot be
    derived from the interval). Reads only the epoch column."""
    reader = csv.reader(lines)
    header = next(reader, None)
    if not header or 'epoch' not in header:
        return []
    col = header.index('epoch')
    epochs = set()
    for row in reader:
        try:
            epoch = int(row[col])
        except (ValueError, TypeError, IndexError):
            continue
        if epoch > 0:
            epochs.add(epoch)
    return sorted(epochs)


@router.get("/{job_id}/snapshots", response_model=SnapshotsListResponse)
async def list_snapshots(job_id: int, current_user: User = Depends(get_current_user)):
    """List available snapshots for a job."""
    try:
        job = Job.get(Job.job_id == job_id)

        if job.status == JobStatus.COMPLETED.value and is_combined_results_file(job.results_file_key):
            logger.info(f"Job {job_id} has combined file, extracting epoch list from it")

            key = job.results_file_key
            verified_epochs = await asyncio.to_thread(lambda: _epochs_in_csv(_iter_s3_lines(key)))

            snapshots = [
                SnapshotInfo(
                    epoch=epoch,
                    file_key=job.results_file_key
                )
                for epoch in verified_epochs
            ]

            return SnapshotsListResponse(
                job_id=job_id,
                snapshot_interval=job.snapshot_interval,
                current_epoch=job.current_epoch,
                snapshots=snapshots
            )

        logger.info(f"Job {job_id} does not have combined file, checking individual snapshot files")
        expected_snapshot_epochs = snapshot_epochs(job.current_epoch, job.snapshot_interval)

        verified_epochs = await verify_snapshots_exist(expected_snapshot_epochs, job_id)

        snapshots = [
            SnapshotInfo(
                epoch=epoch,
                file_key=f'job_id/{job_id}/epoch_num/{epoch}/epoch_results.csv'
            )
            for epoch in verified_epochs
        ]

        if len(verified_epochs) < len(expected_snapshot_epochs):
            missing_count = len(expected_snapshot_epochs) - len(verified_epochs)
            logger.warning(f"Job {job_id}: {missing_count} expected snapshot(s) not found in S3")

        return SnapshotsListResponse(
            job_id=job_id,
            snapshot_interval=job.snapshot_interval,
            current_epoch=job.current_epoch,
            snapshots=snapshots
        )

    except Job.DoesNotExist:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list snapshots for job {job_id}: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to list snapshots")
