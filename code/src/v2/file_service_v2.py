# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
File service for V2 binary snapshots and results.

Uses S3/MinIO for:
- Periodic binary snapshots (for recovery)
- Final CSV results (for dashboard compatibility)
"""

import atexit
import collections
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional

import boto3
import numpy as np
from botocore.exceptions import ClientError

import s3_common
from logging_config import setup_logging
from transient_errors import is_missing_object, is_retryable_s3_error

from .binary_format import BODY_SIZE


logger = setup_logging('file-service-v2')

# The v1 bucket. v2 objects live under V2_PATH_PREFIX in it
EPOCH_RESULTS_BUCKET = s3_common.EPOCH_RESULTS_BUCKET
V2_PATH_PREFIX = 'v2'

_s3_client = None
_s3_client_lock = threading.Lock()


def get_s3_client():
    """Get or create S3 client (thread-safe)."""
    global _s3_client
    if _s3_client is None:
        with _s3_client_lock:
            # Double-check after acquiring lock
            if _s3_client is None:
                _s3_client = boto3.client('s3', config=s3_common.client_config(retries={'max_attempts': 3}),
                                          **s3_common.client_kwargs())
    return _s3_client


def ensure_bucket_exists(bucket_name: str):
    """Create bucket if it doesn't exist."""
    if s3_common.ensure_bucket(get_s3_client(), bucket_name):
        logger.info(f"Created bucket: {bucket_name}")


# Binary Snapshot Operations

def get_snapshot_key(job_id: int, epoch: int) -> str:
    """Generate S3 key for a binary snapshot."""
    return f"{V2_PATH_PREFIX}/job_id/{job_id}/snapshot_{epoch}.bin"


def _put_with_retry(key: str, body: bytes, what: str, max_retries: int = 3, **extra) -> None:
    """put_object, retried with backoff (1, 2 s) on top of botocore's own
    retries: throttling and server faults only, not e.g. a denied request."""
    client = get_s3_client()
    for attempt in range(max_retries):
        try:
            client.put_object(Bucket=EPOCH_RESULTS_BUCKET, Key=key, Body=body, **extra)
            return
        except Exception as e:
            if not is_retryable_s3_error(e) or attempt == max_retries - 1:
                logger.error(f"Upload of {what} failed ({key}): {e}")
                raise
            wait_time = 2 ** attempt
            logger.warning(f"Upload of {what} failed (attempt {attempt + 1}/{max_retries}): {e}. "
                           f"Retrying in {wait_time}s...")
            time.sleep(wait_time)


def upload_snapshot(job_id: int, epoch: int, binary_data: bytes, max_retries: int = 3,
                    generation: Optional[int] = None):
    """Upload a binary snapshot to S3 (retried, see _put_with_retry)."""
    key = get_snapshot_key(job_id, epoch)
    extra = {'Metadata': {'generation': str(generation)}} if generation is not None else {}
    _put_with_retry(key, binary_data, f"snapshot {epoch} of job {job_id}", max_retries,
                    ContentType='application/octet-stream', **extra)
    logger.info(f"Uploaded snapshot: {key} ({len(binary_data)} bytes)")


def get_snapshot_aux_keys(job_id: int, epoch: int) -> tuple:
    """S3 keys of a snapshot's sidecar: (json with the next step, a(0) blob).

    Named outside the snapshot_ prefix so list_snapshots never sees them.
    """
    base = f"{V2_PATH_PREFIX}/job_id/{job_id}"
    return f"{base}/aux_{epoch}.json", f"{base}/accel_{epoch}.bin"


def upload_snapshot_aux(job_id: int, epoch: int, next_dt: float,
                        accelerations: Optional[bytes] = None,
                        generation: Optional[int] = None,
                        simulation_time: Optional[float] = None,
                        dt_used: Optional[float] = None) -> None:
    """Store what a restart from this snapshot needs besides the bodies: the
    step the next epoch uses (adaptive dt) and the shipped a(0) of the
    acceleration-predictor methods. With the returned accelerations of RK4 /
    Forest-Ruth that a(0) is not the exact one, so recomputing it on restart
    would not reproduce the uninterrupted run bitwise. Also the simulation
    time the snapshot's state has reached and the step that reached it,
    for recovery and for the time axis of an adaptive run's analysis."""
    json_key, accel_key = get_snapshot_aux_keys(job_id, epoch)
    # retried like the snapshot: it is written first, and a lost sidecar
    # would cost the whole snapshot
    if accelerations:
        _put_with_retry(accel_key, accelerations, f"a(0) of snapshot {epoch} of job {job_id}",
                        ContentType='application/octet-stream')
    _put_with_retry(
        json_key,
        json.dumps({'epoch': epoch, 'next_dt': repr(float(next_dt)),
                    'simulation_time': None if simulation_time is None else repr(float(simulation_time)),
                    'dt_used': None if dt_used is None else repr(float(dt_used)),
                    'accelerations': bool(accelerations),
                    'generation': generation}).encode(),
        f"sidecar of snapshot {epoch} of job {job_id}",
        ContentType='application/json',
    )


@dataclass(frozen=True)
class SnapshotAux:
    """A snapshot's sidecar. None where the sidecar has no value (written by
    an older revision) or no sidecar exists (written before sidecars)."""
    next_dt: Optional[float] = None
    accelerations: Optional[bytes] = None
    simulation_time: Optional[float] = None
    dt_used: Optional[float] = None


def _get_with_retry(key: str, what: str, max_retries: int = 3) -> Optional[bytes]:
    """get_object's body, retried like _put_with_retry. None if the object
    does not exist, any other error raises."""
    client = get_s3_client()
    for attempt in range(max_retries):
        try:
            return client.get_object(Bucket=EPOCH_RESULTS_BUCKET, Key=key)['Body'].read()
        except Exception as e:
            if is_missing_object(e):
                return None
            if not is_retryable_s3_error(e) or attempt == max_retries - 1:
                logger.error(f"Download of {what} failed ({key}): {e}")
                raise
            wait_time = 2 ** attempt
            logger.warning(f"Download of {what} failed (attempt {attempt + 1}/{max_retries}): {e}. "
                           f"Retrying in {wait_time}s...")
            time.sleep(wait_time)


def _optional_float(value) -> Optional[float]:
    return None if value is None else float(value)


def download_snapshot_aux(job_id: int, epoch: int) -> SnapshotAux:
    """A snapshot's sidecar, read once. An S3 outage raises (after retries):
    recovering without the step and the clock would silently diverge from
    the uninterrupted run. A missing or unreadable sidecar gives an empty
    SnapshotAux, and a failed a(0) read only drops the a(0) (workers then
    recompute it)."""
    json_key, accel_key = get_snapshot_aux_keys(job_id, epoch)
    raw = _get_with_retry(json_key, f"sidecar of snapshot {epoch} of job {job_id}")
    if raw is None:
        logger.info(f"No sidecar for snapshot {epoch} of job {job_id}")
        return SnapshotAux()
    try:
        aux = json.loads(raw)
        next_dt = float(aux['next_dt'])
    except (ValueError, KeyError, TypeError) as e:
        logger.warning(f"Unreadable sidecar for snapshot {epoch} of job {job_id}: {e}")
        return SnapshotAux()
    accel = None
    if aux.get('accelerations'):
        try:
            accel = _get_with_retry(accel_key, f"a(0) of snapshot {epoch} of job {job_id}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not read the a(0) of snapshot {epoch} of job {job_id} "
                           f"(workers will recompute it): {e}")
    return SnapshotAux(next_dt=next_dt, accelerations=accel,
                       simulation_time=_optional_float(aux.get('simulation_time')),
                       dt_used=_optional_float(aux.get('dt_used')))


def download_snapshot(job_id: int, epoch: int) -> bytes:
    """The packed body state of a snapshot."""
    client = get_s3_client()
    key = get_snapshot_key(job_id, epoch)

    response = client.get_object(Bucket=EPOCH_RESULTS_BUCKET, Key=key)
    return response['Body'].read()


def list_snapshots(job_id: int) -> List[int]:
    """The epochs a job has snapshots for, ascending (all pages of the
    listing). Raises ClientError for any S3 error but a missing bucket."""
    client = get_s3_client()
    prefix = f"{V2_PATH_PREFIX}/job_id/{job_id}/snapshot_"

    try:
        epochs = []
        continuation_token = None

        while True:
            list_kwargs = {
                'Bucket': EPOCH_RESULTS_BUCKET,
                'Prefix': prefix
            }
            if continuation_token:
                list_kwargs['ContinuationToken'] = continuation_token

            response = client.list_objects_v2(**list_kwargs)

            for obj in response.get('Contents', []):
                key = obj['Key']
                # Extract epoch from "v2/job_id/123/snapshot_5.bin"
                filename = key.split('/')[-1]  # "snapshot_5.bin"
                epoch_str = filename.replace('snapshot_', '').replace('.bin', '')
                try:
                    epochs.append(int(epoch_str))
                except ValueError:
                    logger.warning(f"Invalid snapshot filename: {key}")
                    continue

            if response.get('IsTruncated'):
                continuation_token = response.get('NextContinuationToken')
            else:
                break

        return sorted(epochs)
    except ClientError as e:
        # Only a missing bucket means no snapshots. Any other error (a
        # throttle, an outage) read as "none" would leave a completed job's
        # metrics empty, and that result is cached for good.
        if str(e.response.get('Error', {}).get('Code', '')) == 'NoSuchBucket':
            return []
        raise


# CSV Results Operations

def get_final_results_key(job_id: int) -> str:
    """Generate S3 key for final CSV results."""
    return f"{V2_PATH_PREFIX}/job_id/{job_id}/final_results.csv"


_FLAT_BODY_DTYPE = np.dtype([('n', '<i4'), ('idBody', '<i4'), ('m', '<f8'), ('x', '<f8'), ('y', '<f8'),
                              ('z', '<f8'), ('vx', '<f8'), ('vy', '<f8'), ('vz', '<f8')])


def final_results_csv(binary_data: bytes, epoch: int) -> str:
    """The final-results CSV (epoch,n,idBody,m,x,y,z,vx,vy,vz) of a packed
    state, byte-identical to what csv.DictWriter writes from per-body dicts
    (floats as repr, CRLF line ends), without building the dicts."""
    rows = np.frombuffer(binary_data, dtype=_FLAT_BODY_DTYPE).tolist()
    if not rows:
        raise ValueError("Cannot create CSV from empty bodies list")
    prefix = f"{epoch},"
    lines = ["epoch,n,idBody,m,x,y,z,vx,vy,vz"]
    lines.extend(prefix + ",".join(map(str, row)) for row in rows)
    return "\r\n".join(lines) + "\r\n"


def upload_final_results_csv(job_id: int, binary_data: bytes, epoch: int) -> str:
    """Upload a packed final state as the job's results CSV (the format the
    dashboard reads), with the final epoch as a column. Returns the S3 key.
    Raises ValueError for empty or malformed data."""
    client = get_s3_client()
    key = get_final_results_key(job_id)

    if not validate_binary_data(binary_data):
        raise ValueError(f"Invalid binary data for job {job_id}: validation failed")

    csv_data = final_results_csv(binary_data, epoch).encode('utf-8')

    client.put_object(
        Bucket=EPOCH_RESULTS_BUCKET,
        Key=key,
        Body=csv_data,
        ContentType='text/csv'
    )
    logger.info(f"Uploaded final CSV results: {key}")
    return key


# Cleanup Operations

def delete_snapshots(job_id: int):
    """Delete all snapshots of a job, with their sidecars (aux_*.json,
    accel_*.bin, see get_snapshot_aux_keys), over all pages of the listing."""
    client = get_s3_client()
    prefix = f"{V2_PATH_PREFIX}/job_id/{job_id}/"

    try:
        continuation_token = None
        deleted_count = 0

        while True:
            list_kwargs = {
                'Bucket': EPOCH_RESULTS_BUCKET,
                'Prefix': prefix
            }
            if continuation_token:
                list_kwargs['ContinuationToken'] = continuation_token

            response = client.list_objects_v2(**list_kwargs)

            # one DeleteObjects request per listed page (at most 1000 keys)
            keys = [{'Key': obj['Key']} for obj in response.get('Contents', [])
                    if obj['Key'].split('/')[-1].startswith(('snapshot_', 'aux_', 'accel_'))]
            if keys:
                result = client.delete_objects(Bucket=EPOCH_RESULTS_BUCKET,
                                               Delete={'Objects': keys, 'Quiet': True})
                for err in result.get('Errors', []):
                    logger.warning(f"Could not delete {err.get('Key')}: {err.get('Message')}")
                deleted_count += len(keys) - len(result.get('Errors', []))

            if response.get('IsTruncated'):
                continuation_token = response.get('NextContinuationToken')
            else:
                break

        if deleted_count > 0:
            logger.info(f"Deleted {deleted_count} snapshots for job {job_id}")

    except ClientError as e:
        logger.error(f"Failed to delete snapshots for job {job_id}: {e}")


# Binary Validation

def validate_binary_data(data: bytes) -> bool:
    """True if data is a non-empty whole number of packed bodies."""
    if not data:
        return False
    if len(data) % BODY_SIZE != 0:
        logger.warning(f"Binary data size {len(data)} is not divisible by body size {BODY_SIZE}")
        return False
    return True


# Async Upload (Non-blocking)

_executor = ThreadPoolExecutor(max_workers=2)

# Snapshot uploads queued or running at once. Each holds a full state (and
# its a(0)): during an S3 outage, when every upload takes until its retries
# give up, an unbounded queue would grow until the worker is killed for
# memory, losing any merge it holds. Past the bound the oldest queued droppable
# (non-final) snapshot of a job with a newer snapshot queued is dropped: that
# later snapshot supersedes it for recovery.
MAX_PENDING_SNAPSHOT_UPLOADS = 4
_pending_uploads: "collections.deque" = collections.deque()
_pending_lock = threading.Lock()


def _cleanup_executor():
    """Let pending uploads finish at process exit. Not logged: at exit the
    logging streams may already be closed (logging then prints an error)."""
    _executor.shutdown(wait=True, cancel_futures=False)


atexit.register(_cleanup_executor)


def upload_snapshot_async(job_id: int, epoch: int, binary_data: bytes,
                          next_dt: Optional[float] = None, accelerations: Optional[bytes] = None,
                          generation: Optional[int] = None, is_current=None,
                          simulation_time: Optional[float] = None, dt_used: Optional[float] = None,
                          droppable: bool = False):
    """Upload a snapshot on a background thread (non-blocking).

    next_dt: the next epoch's step. With it a sidecar is written first (see
    upload_snapshot_aux), so a listed snapshot always has one. accelerations:
    the state's shipped a(0), if any. generation: the generation that produced
    the state, recorded on both objects.

    is_current: a callable returning False once that generation is no longer
    the job's (a recovery happened). Checked before each write: an upload
    delayed in the queue past a recovery must not interleave with the new
    generation's upload of the same epoch, which could pair one run's sidecar
    with the other's snapshot.

    droppable: the upload may be dropped while still queued when
    MAX_PENDING_SNAPSHOT_UPLOADS are pending (a non-final snapshot, superseded
    by later ones). Never for the final snapshot."""
    def _still_current() -> bool:
        if is_current is None:
            return True
        try:
            return bool(is_current())
        except Exception as e:  # noqa: BLE001 — cannot tell: upload rather than lose the snapshot
            logger.warning(f"Could not check the generation of job {job_id} before its snapshot: {e}")
            return True

    def _upload():
        try:
            if not _still_current():
                logger.info(f"Skipping snapshot {epoch} of job {job_id}: generation {generation} is stale")
                return
            if next_dt is not None:
                upload_snapshot_aux(job_id, epoch, next_dt, accelerations, generation=generation,
                                    simulation_time=simulation_time, dt_used=dt_used)
            if not _still_current():
                logger.info(f"Skipping snapshot {epoch} of job {job_id} after its sidecar: "
                            f"generation {generation} is stale")
                return
            upload_snapshot(job_id, epoch, binary_data, generation=generation)
            logger.info(f"Async snapshot upload completed: job {job_id} epoch {epoch}")
        except Exception as e:
            # upload_snapshot has retried already. The next snapshot replaces
            # this one as the job's recovery point
            logger.error(f"Async snapshot upload FAILED for job {job_id} epoch {epoch}: {e}", exc_info=True)

    with _pending_lock:
        # finished uploads anywhere in the queue, not only at its head, so
        # one slow upload cannot make the others count towards the bound
        for f in [f for f in _pending_uploads if f.done()]:
            _pending_uploads.remove(f)
        if len(_pending_uploads) >= MAX_PENDING_SNAPSHOT_UPLOADS:
            # Only a snapshot its own run (job and generation) has a newer
            # upload for, queued or this one, is superseded: not another
            # job's only snapshot, nor, after a recovery to an earlier epoch,
            # the new generation's snapshot by an old one (which is skipped
            # as stale anyway). cancel() only succeeds for a queued upload.
            newest = {}
            for f in list(_pending_uploads):
                run = (f.nbody_job, f.nbody_generation)
                newest[run] = max(newest.get(run, -1), f.nbody_epoch)
            newest[(job_id, generation)] = max(newest.get((job_id, generation), -1), epoch)
            dropped = next((f for f in _pending_uploads
                            if f.nbody_droppable and f.nbody_epoch < newest[(f.nbody_job, f.nbody_generation)]
                            and f.cancel()), None)
            if dropped is not None:
                _pending_uploads.remove(dropped)
                logger.warning(f"Dropped a queued snapshot upload (job {dropped.nbody_job}, epoch "
                               f"{dropped.nbody_epoch}): {MAX_PENDING_SNAPSHOT_UPLOADS} uploads pending")
        future = _executor.submit(_upload)
        future.nbody_job, future.nbody_epoch, future.nbody_droppable = job_id, epoch, droppable
        future.nbody_generation = generation
        _pending_uploads.append(future)
    logger.debug(f"Queued async snapshot upload for job {job_id} epoch {epoch}")
    return future


