# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Queueing the packages of a v1 distributed epoch, and retrying or failing
a v1 step whose message failed.

Shared by the orchestrator (a redelivered FINISHED_EPOCH whose next epoch was
recorded but never published) and the API (resuming a paused job). The
merger and orchestrator share retry_or_fail.
"""
import logging

import pika

from config import settings
from models import Job, Package, utcnow
from rabbitmq import (
    FINISHED_EPOCH_QUEUE,
    GPU_DISTRIBUTED_QUEUE,
    MAX_DELIVERIES,
    NEW_QUEUE,
    TO_MERGE_QUEUE,
    delivery_count,
    retry_later,
)
from schemas import FinishedEpochMessage, JobStatus, MergerMessage, PackageStatus, WorkerMessage


logger = logging.getLogger(__name__)

EPOCH_RESULTS_BUCKET_NAME = settings.s3.epoch_results_bucket
PARTIAL_RESULTS_BUCKET_NAME = settings.s3.partial_results_bucket


def _publish(channel, queue: str, body: str) -> None:
    # mandatory: on the API's confirming channel an unroutable message raises
    # instead of being confirmed and silently dropped
    channel.basic_publish(exchange='', routing_key=queue, body=body,
                          properties=pika.BasicProperties(delivery_mode=2), mandatory=True)


def fail_v1_job(job_id: int, reason: str) -> bool:
    """Mark a v1 job FAILED after its merge or epoch step failed on every
    delivery (never over a final state). Returns False if PostgreSQL could not
    be updated, so the caller keeps the message."""
    logger.error(f"Failing v1 job {job_id}: {reason}")
    final = (JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value)
    try:
        Job.update(status=JobStatus.FAILED.value, updated_at=utcnow()).where(
            (Job.job_id == job_id) & (Job.status.not_in(final))).execute()
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"Could not mark v1 job {job_id} FAILED in PostgreSQL: {e}")
        return False


def retry_or_fail(channel, method, properties, body, queue: str, job_id, step: str, error,
                  count: bool = True) -> None:
    """A v1 merge or epoch step failed: retry it after the retry delay, or
    once it has failed MAX_DELIVERIES times fail its job and drop the message.
    count=False retries without counting (a transient outage, not the job's fault)."""
    deliveries = delivery_count(properties)
    if count and deliveries >= MAX_DELIVERIES:
        if job_id is None or fail_v1_job(job_id, f"{step} failed {deliveries} times, last: {error}"):
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
    retry_later(channel, method.delivery_tag, queue, body, properties, count=count)


def queue_v1_epoch(job, channel, time_step: float = None) -> None:
    """Queue what is left of a v1 job's next epoch (job.current_epoch + 1).

    The epoch may already have package rows: workers finish packages of a
    paused job, and the merger skips a paused job's merge. Rows are reused
    (one per package number), only packages not yet computed are published,
    and when all are computed the merge is triggered. Creating a second set of
    rows instead made the merger's count of TO_MERGE packages overshoot the
    package count, and the job never merged that epoch.

    A job whose last epoch is already recorded (paused right at the end) gets
    its FINISHED_EPOCH re-sent, so the orchestrator completes it.

    time_step defaults to the step the orchestrator recorded for the epoch
    (job.next_time_step), or job.time_step for a job without one.
    """
    if job.current_epoch >= job.num_epochs:
        _publish(channel, FINISHED_EPOCH_QUEUE, FinishedEpochMessage(
            job_id=job.job_id, bucket=EPOCH_RESULTS_BUCKET_NAME, file_key=job.results_file_key or '',
            epoch_num=job.num_epochs, a_max=0.0).to_json())
        logger.info(f"v1 job {job.job_id}: last epoch recorded, completion re-triggered")
        return
    next_epoch = job.current_epoch + 1
    last_file_key = job.results_file_key or job.input_file_key
    worker_queue = GPU_DISTRIBUTED_QUEUE if job.gpu_enabled else NEW_QUEUE
    done = (PackageStatus.TO_MERGE.value, PackageStatus.MERGED.value)
    existing = {}
    for p in Package.select().where((Package.job == job) & (Package.epoch_num == next_epoch)) \
                                .order_by(Package.package_id):
        existing.setdefault(p.package_number, p)
    published = 0
    for i in range(1, job.num_packages + 1):
        package = existing.get(i)
        if package is not None and package.status in done:
            continue
        if package is None:
            package = Package.create(package_number=i, epoch_num=next_epoch, job=job,
                                     status=PackageStatus.IN_PROGRESS.value)
        worker_msg = WorkerMessage.for_package(
            job, next_epoch, i, package.package_id,
            EPOCH_RESULTS_BUCKET_NAME if job.results_file_key else job.input_bucket, last_file_key,
            (job.next_time_step or job.time_step) if time_step is None else time_step)
        _publish(channel, worker_queue, worker_msg.to_json())
        published += 1
    if published == 0:
        # every package was computed while paused: trigger the skipped merge
        first = existing[1]
        _publish(channel, TO_MERGE_QUEUE, MergerMessage(
            job_id=job.job_id, bucket=PARTIAL_RESULTS_BUCKET_NAME, file_key='', epoch_num=next_epoch,
            package_num=1, package_id=first.package_id, package_size=job.package_size).to_json())
    logger.info(f"Queued v1 job {job.job_id} epoch {next_epoch}: published {published} packages"
                + ("" if published else " (all computed, merge triggered)"))
