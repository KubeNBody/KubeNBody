# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
import asyncio
import csv
import io
import json
import logging
import os
import tempfile
import threading
import time
from functools import wraps
from typing import Any, Callable, Dict, Optional, TypeVar

import boto3
import numpy as np
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
from urllib3.exceptions import HTTPError

import s3_common
from config import settings
from transient_errors import is_retryable_s3_error
from utils.file_parsing import BODY_COLUMNS
from utils.snapshot_loader import iter_epochs_pandas


logger = logging.getLogger(__name__)

T = TypeVar('T')

S3_RETRYABLE_EXCEPTIONS = (
    ClientError,
    EndpointConnectionError,
    BotoCoreError,
    HTTPError,
    ConnectionError,
    TimeoutError,
)

def input_csv_reader(text: str) -> csv.DictReader:
    """DictReader for a body CSV as the API validates it (utils.file_parsing):
    spaces after the delimiters and around header names, and the case of the
    names, are ignored, so a file the API accepted ("n, idBody, m, ...",
    "N,IDBODY,...") is read with the same columns."""
    canonical = {c.lower(): c for c in BODY_COLUMNS}
    reader = csv.DictReader(io.StringIO(text), skipinitialspace=True)
    if reader.fieldnames:
        reader.fieldnames = [canonical.get(name.strip().lower(), name.strip()) if name else name
                             for name in reader.fieldnames]
    return reader


def with_s3_retry(max_retries: int = 3, base_delay: float = None, max_delay: float = None):
    """Decorator for S3 operations with exponential backoff retry logic."""
    cfg = settings.s3
    base_delay = base_delay or cfg.retry_base_delay
    max_delay = max_delay or cfg.retry_max_delay

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except S3_RETRYABLE_EXCEPTIONS as e:
                    if not is_retryable_s3_error(e):
                        raise           # e.g. NoSuchKey, 403: retrying cannot help
                    last_exception = e
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                        logger.warning(f"{func.__name__} failed (attempt {attempt}/{max_retries}): {e}. "
                                       f"Retrying in {delay:.1f}s...")
                        time.sleep(delay)
                    else:
                        logger.error(f"{func.__name__} failed after {max_retries} attempts: {e}")
            raise last_exception
        return wrapper
    return decorator

PARTIAL_RESULTS_BUCKET_NAME = s3_common.PARTIAL_RESULTS_BUCKET
EPOCH_RESULTS_BUCKET_NAME = s3_common.EPOCH_RESULTS_BUCKET

def get_s3_connection(max_retries=None, retry_delay=None):
    """A connected boto3 S3 resource, retrying the connection (max_retries
    attempts, retry_delay seconds apart, by default from the config). Raises
    once every attempt failed."""
    cfg = settings.s3
    max_retries = max_retries or cfg.max_retries
    retry_delay = retry_delay or 2

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Attempting to connect to MinIO/S3 at {cfg.endpoint_url} (attempt {attempt}/{max_retries})")
            s3 = boto3.resource('s3', config=s3_common.client_config(), **s3_common.client_kwargs())
            # Test the connection by listing buckets
            list(s3.buckets.all())
            logger.info("Successfully connected to MinIO/S3")
            return s3
        except S3_RETRYABLE_EXCEPTIONS as e:
            if not is_retryable_s3_error(e):
                raise           # bad credentials or region: retrying cannot help
            if attempt < max_retries:
                logger.warning(f"Failed to connect to MinIO/S3: {e}. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error(f"Failed to connect to MinIO/S3 after {max_retries} attempts")
                raise ConnectionError(f"Could not connect to MinIO/S3 at {cfg.endpoint_url} after "
                                      f"{max_retries} attempts") from e

# Lazy S3 connection - initialized on first use
_s3_connection = None
_s3_lock = threading.Lock()


def get_s3():
    """Get or create S3 connection (thread-safe lazy initialization)."""
    global _s3_connection
    if _s3_connection is None:
        with _s3_lock:
            if _s3_connection is None:
                _s3_connection = get_s3_connection()
    return _s3_connection

def ensure_buckets_exist():
    """Ensure required buckets exist, create them if they don't."""
    required_buckets = [PARTIAL_RESULTS_BUCKET_NAME, EPOCH_RESULTS_BUCKET_NAME]
    s3 = get_s3()

    for bucket_name in required_buckets:
        try:
            created = s3_common.ensure_bucket(s3.meta.client, bucket_name)
            logger.info(f"{'Created' if created else 'Found'} bucket '{bucket_name}'")
        except ClientError as e:
            logger.error(f"Could not check or create bucket '{bucket_name}': {e}")


@with_s3_retry(max_retries=3, base_delay=1.0)
def download_file(bucket_name: str, key: str):
    """Download file from S3 with retry logic."""
    response = get_s3().Object(bucket_name, key).get()
    return response['Body'].read().decode('utf-8')


@with_s3_retry(max_retries=3, base_delay=1.0)
def upload_file(bucket_name, key, f):
    """Upload file to S3 with retry logic."""
    return get_s3().Bucket(bucket_name).put_object(Body=f.getvalue(), Key=key)


def verify_file_exists(bucket_name: str, key: str) -> bool:
    """Verify that a file exists in S3."""
    try:
        get_s3().meta.client.head_object(Bucket=bucket_name, Key=key)
        return True
    except ClientError as e:
        logger.debug(f"File not found in S3: {bucket_name}/{key} - {e}")
        return False
    except Exception as e:
        logger.error(f"Error checking file existence: {bucket_name}/{key} - {e}")
        return False

def combine_epoch_snapshots(job_id: int, snapshot_interval: int, num_epochs: int) -> str:
    """Combine the job's epoch snapshots into one results file. Returns its key."""
    logger.info(f"Combining epoch snapshots for job {job_id}")

    # Calculate which epochs should have snapshots
    # (arithmetic, not a modulo over every epoch: an interval of 0 means the
    # final epoch only, as in utils.job_helpers.snapshot_epochs)
    snapshot_epochs = list(range(snapshot_interval, num_epochs + 1, snapshot_interval)) \
        if snapshot_interval > 0 else []
    if num_epochs > 0 and (not snapshot_epochs or snapshot_epochs[-1] != num_epochs):
        snapshot_epochs.append(num_epochs)

    logger.info(f"Found {len(snapshot_epochs)} snapshot epochs to combine")

    # Stream snapshots to S3 via multipart upload — only one snapshot in memory at a time
    combined_file_key = f'job_id/{job_id}/combined_results.csv'
    s3_client = get_s3().meta.client
    mpu = s3_client.create_multipart_upload(Bucket=EPOCH_RESULTS_BUCKET_NAME, Key=combined_file_key)
    upload_id = mpu['UploadId']

    parts = []
    part_buffer = io.BytesIO()
    header_written = False
    snapshots_combined = 0
    missing = 0
    part_number = 1
    MIN_PART_SIZE = 5 * 1024 * 1024  # S3 minimum part size: 5MB

    try:
        for epoch_num in snapshot_epochs:
            file_key = f'job_id/{job_id}/epoch_num/{epoch_num}/epoch_results.csv'

            try:
                content = download_file(EPOCH_RESULTS_BUCKET_NAME, file_key)
                lines = [line.strip() for line in content.strip().split('\n') if line.strip()]

                if not lines:
                    continue

                if not header_written:
                    part_buffer.write((lines[0] + '\n').encode('utf-8'))
                    header_written = True

                for line in lines[1:]:
                    part_buffer.write((line + '\n').encode('utf-8'))

                snapshots_combined += 1

                # Flush part when buffer exceeds minimum part size
                if part_buffer.tell() >= MIN_PART_SIZE:
                    part_buffer.seek(0)
                    resp = s3_client.upload_part(
                        Bucket=EPOCH_RESULTS_BUCKET_NAME, Key=combined_file_key,
                        UploadId=upload_id, PartNumber=part_number, Body=part_buffer.read()
                    )
                    parts.append({'ETag': resp['ETag'], 'PartNumber': part_number})
                    part_number += 1
                    part_buffer = io.BytesIO()

            except ClientError as e:
                if e.response.get('Error', {}).get('Code') not in ('NoSuchKey', '404'):
                    raise
                # never written (e.g. a recovered job): nothing to combine. Any
                # other error is raised, since the per-epoch files are deleted
                # once the combined file exists and a skipped epoch would be lost.
                logger.warning(f"Snapshot of epoch {epoch_num} of job {job_id} does not exist, skipped")
                missing += 1

        if not header_written:
            s3_client.abort_multipart_upload(
                Bucket=EPOCH_RESULTS_BUCKET_NAME, Key=combined_file_key, UploadId=upload_id
            )
            raise Exception(f"No valid snapshots found for job {job_id}")

        # Upload remaining data as final part
        if part_buffer.tell() > 0:
            part_buffer.seek(0)
            resp = s3_client.upload_part(
                Bucket=EPOCH_RESULTS_BUCKET_NAME, Key=combined_file_key,
                UploadId=upload_id, PartNumber=part_number, Body=part_buffer.read()
            )
            parts.append({'ETag': resp['ETag'], 'PartNumber': part_number})

        s3_client.complete_multipart_upload(
            Bucket=EPOCH_RESULTS_BUCKET_NAME, Key=combined_file_key,
            UploadId=upload_id, MultipartUpload={'Parts': parts}
        )

    except Exception:
        try:
            s3_client.abort_multipart_upload(
                Bucket=EPOCH_RESULTS_BUCKET_NAME, Key=combined_file_key, UploadId=upload_id
            )
        except Exception:
            pass
        raise

    logger.info(f"Successfully combined {snapshots_combined} snapshots into {combined_file_key}"
                + (f" ({missing} missing)" if missing else ""))

    # Generate animation file — estimate size first to avoid loading huge files
    try:
        # Rough estimate: 40 bytes per particle per snapshot in JSON
        num_bodies = 0
        first_key = f'job_id/{job_id}/epoch_num/{snapshot_epochs[0]}/epoch_results.csv'
        try:
            first_content = download_file(EPOCH_RESULTS_BUCKET_NAME, first_key)
            num_bodies = max(first_content.strip().count('\n'), 1)  # lines minus header ≈ bodies
        except Exception:
            pass
        estimated_anim_mb = (snapshots_combined * num_bodies * 40) / (1024 * 1024)

        if estimated_anim_mb > MAX_ANIMATION_FILE_SIZE_MB:
            logger.info(
                f"Skipping animation generation for job {job_id}: estimated ~{estimated_anim_mb:.0f}MB "
                f"exceeds {MAX_ANIMATION_FILE_SIZE_MB}MB limit. Viewer will use paginated loading."
            )
        else:
            generate_animation_file_from_s3(job_id, combined_file_key)
            logger.info(f"Successfully generated animation file for job {job_id}")
    except Exception as anim_e:
        logger.warning(f"Failed to generate animation file for job {job_id}: {anim_e}")

    return combined_file_key


MAX_ANIMATION_FILE_SIZE_MB = 50  # Skip generation if estimated size > 50MB (viewer falls back to paginated loading)


def get_animation_file_key(job_id: int) -> str:
    """Get the expected animation file key for a job."""
    return f'job_id/{job_id}/animation_data.json'


def load_animation_data(job_id: int) -> Optional[Dict[str, Any]]:
    """The job's pre-generated animation data, None if the file is missing or
    invalid."""
    animation_file_key = get_animation_file_key(job_id)

    try:
        content = download_file(EPOCH_RESULTS_BUCKET_NAME, animation_file_key)
        data = json.loads(content)

        # Validate structure
        if not isinstance(data, dict):
            logger.warning(f"Animation file for job {job_id} is not a dict")
            return None

        snapshots = data.get("snapshots")
        if not isinstance(snapshots, list):
            logger.warning(f"Animation file for job {job_id} has invalid snapshots field")
            return None

        # Basic validation of first snapshot if present
        if snapshots and len(snapshots) > 0:
            first = snapshots[0]
            if not isinstance(first, dict) or "epoch" not in first or "particles" not in first:
                logger.warning(f"Animation file for job {job_id} has malformed snapshot")
                return None

        return data
    except Exception as e:
        logger.debug(f"Animation file not found or invalid for job {job_id}: {e}")
        return None


async def load_animation_data_async(job_id: int) -> Optional[Dict[str, Any]]:
    """Async wrapper for load_animation_data to avoid blocking event loop."""
    return await asyncio.to_thread(load_animation_data, job_id)


def generate_animation_file_from_path(job_id: int, csv_path: str) -> str:
    """Generate the job's animation JSON by streaming the combined CSV at
    csv_path. Returns its key.

    Parses one epoch at a time (pandas chunked reader), appends each
    snapshot to a JSON file on disk, and uploads from disk: peak memory is
    one epoch and one parser chunk, whatever the job's size (the whole CSV
    and the JSON in memory took ~2 GB for a 300 MB CSV).

    total_snapshots is written AFTER the snapshots array (key order is
    irrelevant to JSON parsers) because the count isn't known until the stream
    is exhausted. ValueError if the animation exceeds
    MAX_ANIMATION_FILE_SIZE_MB."""


    logger.info(f"Generating animation data file for job {job_id} (streaming)")

    max_bytes = MAX_ANIMATION_FILE_SIZE_MB * 1024 * 1024
    fd, out_path = tempfile.mkstemp(prefix='nbody-anim-', suffix='.json')
    snapshots_written = 0
    try:
        with os.fdopen(fd, 'w') as out:
            out.write('{"job_id":%d,"snapshots":[' % job_id)

            for epoch, (positions, _velocities, masses) in iter_epochs_pandas(csv_path):
                total_mass = float(masses.sum())
                if total_mass > 0:
                    com = (positions * masses[:, None]).sum(axis=0) / total_mass
                else:
                    com = np.zeros(3)

                # One row per particle: [x, y, z, mass]
                particles = np.column_stack([positions, masses])
                snapshot = {
                    "epoch": int(epoch),
                    "particles": particles.tolist(),
                    "center_of_mass": [float(com[0]), float(com[1]), float(com[2])],
                }
                if snapshots_written:
                    out.write(',')
                json.dump(snapshot, out, separators=(',', ':'))
                snapshots_written += 1

                if out.tell() > max_bytes:
                    raise ValueError(
                        f"Animation too large: exceeds {MAX_ANIMATION_FILE_SIZE_MB}MB limit "
                        f"(at snapshot {snapshots_written})"
                    )

            out.write('],"total_snapshots":%d}' % snapshots_written)

        animation_file_key = get_animation_file_key(job_id)
        # boto3 managed transfer streams from disk (no in-memory copy)
        get_s3().Bucket(EPOCH_RESULTS_BUCKET_NAME).upload_file(out_path, animation_file_key)

        if not verify_file_exists(EPOCH_RESULTS_BUCKET_NAME, animation_file_key):
            raise Exception(f"Failed to verify upload of animation file {animation_file_key}")

        logger.info(f"Successfully generated animation file for job {job_id} with {snapshots_written} snapshots")
        return animation_file_key
    finally:
        if os.path.exists(out_path):
            os.unlink(out_path)


def generate_animation_file_from_s3(job_id: int, csv_key: str) -> str:
    """generate_animation_file_from_path for a combined CSV in the epoch
    results bucket, downloaded to a temporary file first."""
    fd, csv_path = tempfile.mkstemp(prefix='nbody-combined-', suffix='.csv')
    os.close(fd)
    try:
        get_s3().Bucket(EPOCH_RESULTS_BUCKET_NAME).download_file(csv_key, csv_path)
        return generate_animation_file_from_path(job_id, csv_path)
    finally:
        os.unlink(csv_path)


async def generate_animation_file_from_path_async(job_id: int, csv_path: str) -> str:
    """Async wrapper for generate_animation_file_from_path."""
    return await asyncio.to_thread(generate_animation_file_from_path, job_id, csv_path)


def delete_file(bucket_name: str, key: str) -> None:
    """Delete the object at exactly this key."""
    s3 = get_s3()
    s3.Object(bucket_name, key).delete()


def delete_files_by_prefix(bucket_name: str, prefix: str) -> int:
    """Delete every object under prefix (e.g. 'job_id/123/'). Returns how many.
    ValueError for an empty prefix, which would delete the whole bucket."""
    # Safety check: prevent accidental deletion of entire bucket
    if not prefix or prefix.strip() == '':
        raise ValueError(
            "Prefix cannot be empty - this would delete all files in the bucket. "
            "Please provide a specific prefix to delete."
        )

    # Additional safety: check for root directory
    if prefix.strip() == '/':
        raise ValueError(
            "Prefix cannot be root directory '/' - this would delete all files in the bucket."
        )

    # Additional safety: ensure prefix is job-specific
    if not prefix.startswith('job_id/'):
        raise ValueError(
            f"Invalid prefix format: '{prefix}'. Must start with 'job_id/' to ensure job-specific deletion."
        )

    # Additional safety: validate that job ID exists in prefix and is numeric
    # Expected format: "job_id/{numeric_job_id}/" or "job_id/{numeric_job_id}/..."
    # This prevents "job_id/" (no ID) from deleting all jobs
    parts = prefix.split('/')
    if len(parts) < 2 or not parts[1] or not parts[1].isdigit():
        raise ValueError(
            f"Invalid prefix format: '{prefix}'. Must be 'job_id/{{numeric_id}}/...' with a valid numeric job ID. "
            f"Got parts: {parts}"
        )

    try:
        s3 = get_s3()
        try:
            s3.meta.client.head_bucket(Bucket=bucket_name)
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code == '404':
                logger.warning(f"Bucket '{bucket_name}' does not exist, skipping deletion")
                return 0
            else:
                raise

        deleted_count = 0
        batch_size = 1000
        MAX_ITERATIONS = 10000

        paginator = s3.meta.client.get_paginator('list_objects_v2')
        page_iterator = paginator.paginate(
            Bucket=bucket_name,
            Prefix=prefix,
            PaginationConfig={'PageSize': batch_size}
        )

        iteration_count = 0
        for page in page_iterator:
            iteration_count += 1

            if iteration_count > MAX_ITERATIONS:
                raise Exception(
                    f"Exceeded maximum iterations ({MAX_ITERATIONS}) while deleting files with prefix '{prefix}'. "
                    f"Possible infinite loop or extremely large dataset. Deleted {deleted_count} files so far."
                )

            if 'Contents' not in page or not page['Contents']:
                continue

            delete_keys = [{'Key': obj['Key']} for obj in page['Contents']]

            response = s3.meta.client.delete_objects(
                Bucket=bucket_name,
                Delete={'Objects': delete_keys}
            )

            # Track successful deletions
            deleted_in_batch = len(response.get('Deleted', []))
            deleted_count += deleted_in_batch

            if 'Errors' in response and response['Errors']:
                error_details = ' | '.join([
                    f"{e['Key']}: {e['Message']}"
                    for e in response['Errors']
                ])
                raise Exception(
                    f"Failed to delete {len(response['Errors'])} file(s) from bucket '{bucket_name}': {error_details}"
                )

        if deleted_count == 0:
            logger.info(f"No files found in bucket '{bucket_name}' with prefix '{prefix}'")
        else:
            logger.info(f"Deleted {deleted_count} files from bucket '{bucket_name}' with prefix '{prefix}'")

        return deleted_count

    except Exception as e:
        logger.error(f"Error deleting files from bucket '{bucket_name}' with prefix '{prefix}': {e}")
        raise
