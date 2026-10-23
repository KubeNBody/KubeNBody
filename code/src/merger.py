# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
import asyncio
import csv
import io
import json
import time
from typing import Any, Dict, List

import numpy as np
import pika

from async_file_service import EPOCH_RESULTS_BUCKET_NAME, PARTIAL_RESULTS_BUCKET_NAME, AsyncS3Service
from constants import (
    MERGE_DOWNLOAD_TIMEOUT_SECONDS,
    RABBITMQ_RECONNECT_DELAY_SECONDS,
    SOFTENING_SQ,
    WORKER_PREFETCH_COUNT,
)
from file_service import ensure_buckets_exist, upload_file
from job_timestamps import wait_for_job_schema
from logging_config import setup_logging
from models import Job, Package, db
from physics import compute_accelerations
from rabbitmq import (
    FINISHED_EPOCH_QUEUE,
    RABBITMQ_CHANNEL_ERRORS,
    TO_MERGE_QUEUE,
    close_quietly,
    ensure_rabbitmq_queues,
    get_rabbitmq_connection,
)
from schemas import FinishedEpochMessage, JobStatus, MergerMessage, PackageStatus
from tracing import annotate, setup_tracing, traced
from transient_errors import is_retryable_s3_error, is_transient_error
from v1_epochs import retry_or_fail


logger = setup_logging('merger')
setup_tracing('nbody-merger')

logger.info("Initializing Merger service...")
ensure_buckets_exist()

channel = None


def get_final_results_file(results: List[Dict[str, Any]], epoch_num: int) -> str:
    """The epoch's results as CSV text, ordered. ValueError for no results."""
    if not results:
        raise ValueError("Cannot create CSV from empty results list")

    # Sort by the numeric ordinal n (not lexicographically)
    sorted_results = sorted(results, key=lambda x: int(x['n']))

    # Create new dicts with epoch column (avoid mutating original data)
    results_with_epoch = [
        {**entry, 'epoch': epoch_num}
        for entry in sorted_results
    ]

    # Define fieldnames with epoch as first column
    fieldnames = ['epoch', 'n', 'idBody', 'm', 'x', 'y', 'z', 'vx', 'vy', 'vz']

    with io.StringIO() as file_buffer:
        writer = csv.DictWriter(file_buffer, fieldnames=fieldnames)
        writer.writeheader()
        for entry in results_with_epoch:
            writer.writerow(entry)
        return file_buffer.getvalue()


async def download_and_parse_package(
    service: AsyncS3Service,
    package: Package
) -> List[Dict[str, Any]]:
    """The bodies of a package's results file, downloaded asynchronously."""
    try:
        raw_file = await service.download_file(PARTIAL_RESULTS_BUCKET_NAME, package.file_key)
        csv_reader = csv.DictReader(io.StringIO(raw_file))
        results = list(csv_reader)
        logger.debug(f'Downloaded {len(results)} bodies from package {package.package_id}')
        return results
    except Exception as e:
        logger.error(f'Failed to download package {package.package_id}: {e}')
        raise


class PackageDownloadError(RuntimeError):
    """Some package results could not be downloaded. transient: every
    failure was an S3 availability error, so the merge is retried without
    counting towards the job's failure cap."""

    def __init__(self, message: str, transient: bool):
        super().__init__(message)
        self.transient = transient


async def download_packages_async(
    packages: List[Package],
    timeout_seconds: int = MERGE_DOWNLOAD_TIMEOUT_SECONDS
) -> List[Dict[str, Any]]:
    """Download multiple package results concurrently using asyncio."""
    try:
        async with AsyncS3Service() as service:     # one client for all packages
            tasks = [download_and_parse_package(service, package) for package in packages]
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout_seconds
            )
    except asyncio.TimeoutError:
        # A hung (not refused) S3 endpoint: an outage like any other
        raise PackageDownloadError(f"Download timed out after {timeout_seconds} seconds", transient=True)

    final_results = []
    failed_packages = []

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            failed_packages.append((packages[i].package_id, result))
        else:
            final_results.extend(result)

    if failed_packages:
        failed_ids = [f"pkg_{pkg_id}: {err}" for pkg_id, err in failed_packages]
        raise PackageDownloadError(
            f"Failed to download {len(failed_packages)} packages: {', '.join(failed_ids)}",
            transient=all(is_retryable_s3_error(err) for _, err in failed_packages))

    return final_results


@traced('merge epoch')
def process_merge(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    properties: pika.spec.BasicProperties,
    body: bytes
) -> None:
    message = None

    try:
        message = MergerMessage.from_json(body)
        annotate(job_id=message.job_id, epoch=message.epoch_num)
        logger.info(f'Processing merge for job {message.job_id}, epoch {message.epoch_num}')

        job = Job.get(Job.job_id == message.job_id)

        if job.status in [JobStatus.CANCELLED.value, JobStatus.PAUSED.value]:
            logger.info(f'Job {message.job_id} is {job.status}, skipping merge')
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        # this epoch's packages only: rows of another epoch waiting to merge
        # (e.g. left by a pause) must not make the count reach, or overshoot,
        # num_packages
        packages = list(Package.select().where(
            Package.job == job,
            Package.epoch_num == message.epoch_num,
            Package.status == PackageStatus.TO_MERGE.value
        ))

        if len(packages) != job.num_packages:
            logger.debug(f'Only {len(packages)}/{job.num_packages} packages ready, waiting for more')
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        logger.info(f'All {len(packages)} partial results ready for merge')
        logger.debug('Downloading and combining partial results concurrently with asyncio')

        final_results = asyncio.run(download_packages_async(packages))

        logger.debug(f'Downloaded {len(final_results)} bodies from {len(packages)} packages')

        csv_content = get_final_results_file(final_results, message.epoch_num)
        logger.debug('Finished combining results')

        file_key = f'job_id/{message.job_id}/epoch_num/{message.epoch_num}/epoch_results.csv'
        upload_file(EPOCH_RESULTS_BUCKET_NAME, file_key, io.StringIO(csv_content))
        logger.info(f'Uploaded epoch results to {file_key}')

        # Compute global a_max for adaptive timestepping (from merged body state)
        global_a_max = 0.0
        if job.adaptive_enabled and final_results:
            try:
                n = len(final_results)
                positions = np.zeros((n, 3), dtype=np.float64)
                masses = np.zeros(n, dtype=np.float64)
                ids = np.zeros(n, dtype=np.int32)
                for i, body in enumerate(final_results):
                    positions[i] = [float(body['x']), float(body['y']), float(body['z'])]
                    masses[i] = float(body['m'])
                    ids[i] = int(body['idBody'])
                # Use job's softening_sq so a_max reflects the same physics
                # the workers used. Falling back to module default if absent.
                job_softening_sq = job.softening_sq or SOFTENING_SQ
                accel = compute_accelerations(positions, masses, ids, softening_sq=job_softening_sq)
                global_a_max = float(np.max(np.sqrt(np.sum(accel**2, axis=1))))
                logger.info(f'Adaptive: computed a_max={global_a_max:.4e} from {n} merged bodies')
            except Exception as e:
                logger.error(f'Failed to compute a_max for adaptive dt: {e}', exc_info=True)
                logger.error('Adaptive dt: a_max unknown (sent as 0), the next epoch keeps the current step')

        logger.debug('Publishing finished epoch message')
        finished_msg = FinishedEpochMessage(
            job_id=message.job_id,
            bucket=EPOCH_RESULTS_BUCKET_NAME,
            file_key=file_key,
            epoch_num=message.epoch_num,
            a_max=global_a_max,
        )

        # Publish before marking the packages MERGED: a crash in between then
        # leaves them TO_MERGE, so the redelivered message merges again and
        # re-publishes (the orchestrator drops the duplicate). The other order
        # left a redelivery with nothing to merge and the job stalled.
        channel.basic_publish(
            exchange='',
            routing_key=FINISHED_EPOCH_QUEUE,
            body=finished_msg.to_json(),
            properties=pika.BasicProperties(delivery_mode=2)
        )

        with db.atomic():
            Package.update(status=PackageStatus.MERGED.value).where(
                Package.package_id.in_([p.package_id for p in packages])
            ).execute()

        ch.basic_ack(delivery_tag=method.delivery_tag)
        logger.info(f"Successfully merged epoch {message.epoch_num} for job {message.job_id}")

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse message body: {e}")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    except Job.DoesNotExist:
        job_id = message.job_id if message else "unknown"
        logger.error(f"Job {job_id} not found in database")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    except RABBITMQ_CHANNEL_ERRORS:
        raise                           # main() reconnects. The broker redelivers the message

    except Exception as e:
        logger.error(f"Failed to process merge: {e}", exc_info=True)
        # An S3 or PostgreSQL outage is not the job's fault: retried without
        # counting, so a long outage delays the merge but cannot fail the job
        transient = getattr(e, 'transient', False) or is_transient_error(e)
        retry_or_fail(ch, method, properties, body, TO_MERGE_QUEUE,
                      message.job_id if message else None, f"Merge of epoch "
                      f"{message.epoch_num if message else '?'}", e, count=not transient)

def main():
    global channel

    # Wait for the API's schema migration (new model columns) before consuming
    wait_for_job_schema()

    while True:
        connection = None
        try:
            connection = get_rabbitmq_connection()
            channel = connection.channel()
            ensure_rabbitmq_queues(channel)
            # basic_publish returns once the broker has the message, and raises
            # (NackError, UnroutableError) when it refuses it: the delivery being
            # processed is then not acknowledged, and its redelivery publishes again
            channel.confirm_delivery()

            channel.basic_qos(prefetch_count=WORKER_PREFETCH_COUNT)
            channel.basic_consume(
                queue=TO_MERGE_QUEUE,
                auto_ack=False,
                on_message_callback=process_merge
            )

            logger.info("Merger ready, waiting for packages to merge...")
            channel.start_consuming()

        except RABBITMQ_CHANNEL_ERRORS as e:
            logger.warning(f"Connection lost: {e}. Reconnecting in {RABBITMQ_RECONNECT_DELAY_SECONDS} seconds...")
            close_quietly(connection)
            time.sleep(RABBITMQ_RECONNECT_DELAY_SECONDS)
        except KeyboardInterrupt:
            logger.info("Shutting down merger...")
            break


if __name__ == '__main__':
    main()
