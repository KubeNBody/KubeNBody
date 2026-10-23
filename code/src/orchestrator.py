# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
import json
import time

import pika

from constants import RABBITMQ_RECONNECT_DELAY_SECONDS, WORKER_PREFETCH_COUNT
from file_service import (
    EPOCH_RESULTS_BUCKET_NAME,
    combine_epoch_snapshots,
    delete_files_by_prefix,
    ensure_buckets_exist,
    is_retryable_s3_error,
)
from job_timestamps import wait_for_job_schema
from logging_config import setup_logging
from models import Job, Package, db, utcnow
from physics import estimate_adaptive_dt_from_amax
from rabbitmq import (
    FINISHED_EPOCH_QUEUE,
    GPU_DISTRIBUTED_QUEUE,
    NEW_QUEUE,
    RABBITMQ_CHANNEL_ERRORS,
    close_quietly,
    delivery_count,
    ensure_rabbitmq_queues,
    get_rabbitmq_connection,
)
from schemas import FinishedEpochMessage, JobStatus, PackageStatus, WorkerMessage
from tracing import annotate, setup_tracing, traced
from transient_errors import is_transient_error
from v1_epochs import queue_v1_epoch, retry_or_fail


logger = setup_logging('orchestrator')
setup_tracing('nbody-orchestrator')

logger.info("Initializing Orchestrator service...")
ensure_buckets_exist()

channel = None


def _complete_job(job) -> None:
    """Combine a finished job's snapshots and mark it COMPLETED (outside the
    epoch transaction). The per-epoch snapshots are deleted only once the job
    points at the combined file, so a crash in between leaves them, and a
    redelivered message, to finish the completion."""
    results_key = job.results_file_key
    logger.info(f'Job {job.job_id} completed! Combining all snapshots into single file...')
    combined = None
    try:
        combined = combine_epoch_snapshots(job_id=job.job_id, snapshot_interval=job.snapshot_interval,
                                           num_epochs=job.num_epochs)
        results_key = combined
        logger.info(f'Combined results available at: {combined}')
    except Exception as e:
        if is_retryable_s3_error(e):
            # S3 unavailable (download_file has retried already): leave the job
            # IN_PROGRESS and the message to be redelivered, which resumes the
            # completion. Marking it COMPLETED would never combine it again
            raise
        logger.error(f'Failed to combine snapshots for job {job.job_id}: {e}', exc_info=True)
        logger.warning('Job marked as complete but snapshot combination failed.')
    # completed_at: the end of the integration (the last epoch was recorded
    # with updated_at), as for the other engines, not of the combination
    updated = Job.update(status=JobStatus.COMPLETED.value, results_file_key=results_key,
                         updated_at=utcnow(), completed_at=job.updated_at or utcnow()).where(
        (Job.job_id == job.job_id) & (Job.status == JobStatus.IN_PROGRESS.value)).execute()
    if not updated:
        # paused or cancelled during the combination: the job still points at
        # the last epoch's file, which a resume re-combines from (its
        # FINISHED_EPOCH is re-sent by queue_v1_epoch), so keep the snapshots
        logger.info(f'Job {job.job_id} left IN_PROGRESS during the combination, '
                    f'not completed, snapshots kept')
        return
    if combined:
        logger.info(f'Cleaning up individual snapshot files for job {job.job_id}')
        deleted = delete_files_by_prefix(EPOCH_RESULTS_BUCKET_NAME, f'job_id/{job.job_id}/epoch_num/')
        logger.info(f'Deleted {deleted} snapshot files')


def _next_time_step(job, a_max: float) -> float:
    """Step of the next epoch: the job's step, or for an adaptive job the one
    estimated from the merged state's maximum acceleration. a_max 0 means the
    merger could not compute it: the job keeps its current step. A NaN (a
    diverged state) gets the smallest step, as in the monolithic and v2 paths."""
    if not job.adaptive_enabled or a_max == 0:
        return job.time_step
    dt = estimate_adaptive_dt_from_amax(
        a_max, job.time_step,
        eta=job.adaptive_eta,
        softening=job.softening_sq ** 0.5,
        dt_min=job.adaptive_dt_min or 1e-10,
        dt_max=job.adaptive_dt_max or job.time_step,
    )
    logger.info(f'Adaptive dt for job {job.job_id}: a_max={a_max:.4e} -> dt={dt:.4e}')
    return dt


@traced('finalize epoch')
def process_finalize_epoch(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    properties: pika.spec.BasicProperties,
    body: bytes
) -> None:
    """Process a finished epoch's message and start the next epoch.

    current_epoch counts the fully completed epochs: once it reaches
    num_of_epochs the job is done, else packages are created for epoch
    current_epoch + 1 (epochs are numbered from 1)."""
    message = None

    try:
        message = FinishedEpochMessage.from_json(body)
        annotate(job_id=message.job_id, epoch=message.epoch_num)
        logger.info(f'Finalizing epoch {message.epoch_num} for job {message.job_id}')

        messages_to_publish = []
        job_completed = False
        requeue_next = False

        with db.atomic():
            job = Job.select().where(Job.job_id == message.job_id).for_update().get()

            # Idempotency: a duplicate or out-of-order FINISHED_EPOCH message
            # (merger redelivery after a crash-before-ack) must not advance
            # the epoch counter twice — that would skip an epoch's packages.
            if message.epoch_num != job.current_epoch + 1:
                if (message.epoch_num == job.current_epoch == job.num_epochs
                        and job.status == JobStatus.IN_PROGRESS.value):
                    # the last epoch was recorded but completion was interrupted
                    # (combination runs outside this transaction): finish it
                    job_completed = True
                elif (message.epoch_num == job.current_epoch
                        and job.status == JobStatus.IN_PROGRESS.value
                        and (getattr(method, 'redelivered', False)
                             or delivery_count(properties) > 1)):
                    # our own earlier delivery (redelivered, or a retry_later copy) recorded the epoch but its
                    # successor's publish failed or was cut short by a crash:
                    # queue what is not computed. (A fresh duplicate, e.g. the
                    # merger re-publishing after a crash, falls through: the
                    # epoch it reports was published then.)
                    requeue_next = True
                else:
                    logger.warning(
                        f'Job {message.job_id}: ignoring FINISHED_EPOCH for epoch {message.epoch_num} '
                        f'(current_epoch={job.current_epoch}, expected {job.current_epoch + 1}) '
                        f'— duplicate or out-of-order delivery'
                    )
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    return

            if not (job_completed or requeue_next):  # (a resumed step records nothing)
                job.results_file_key = message.file_key
                job.current_epoch += 1

            if job.status in [JobStatus.PAUSED.value, JobStatus.CANCELLED.value]:
                logger.info(f'Job {message.job_id} is {job.status} at epoch {job.current_epoch}, stopping')
                if (job.adaptive_enabled and not (job_completed or requeue_next)
                        and job.current_epoch < job.num_epochs):
                    # the step of the epoch the resume starts, from this one's a_max
                    job.next_time_step = _next_time_step(job, message.a_max)
                job.save()
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return

            if requeue_next:
                pass                            # queued after the transaction
            elif job.current_epoch == job.num_epochs:
                # Record the last epoch now. Combine the snapshots after the
                # transaction, so the row lock is not held for the whole
                # combination (API cancel/pause updates would wait on it).
                job_completed = True
                job.updated_at = utcnow()
                job.save()
            else:
                next_epoch = job.current_epoch + 1
                logger.info(f'Starting epoch {next_epoch}/{job.num_epochs} for job {message.job_id}')

                next_time_step = _next_time_step(job, message.a_max)
                job.next_time_step = next_time_step

                for i in range(1, job.num_packages + 1):
                    package = Package(
                        package_number=i,
                        epoch_num=next_epoch,
                        job=job,
                        status=PackageStatus.IN_PROGRESS.value
                    )
                    package.save()

                    worker_msg = WorkerMessage.for_package(
                        job, next_epoch, i, package.package_id, message.bucket, message.file_key,
                        next_time_step)
                    messages_to_publish.append(worker_msg)

                job.save()

        if job_completed:
            _complete_job(job)
        elif requeue_next:
            logger.warning(f'Job {message.job_id}: epoch {message.epoch_num} redelivered after it '
                           f'was recorded, re-queueing the unfinished packages of the next epoch')
            queue_v1_epoch(job, channel, _next_time_step(job, message.a_max))
        elif messages_to_publish:
            worker_queue = GPU_DISTRIBUTED_QUEUE if job.gpu_enabled else NEW_QUEUE
            for worker_msg in messages_to_publish:
                channel.basic_publish(
                    exchange='',
                    routing_key=worker_queue,
                    body=worker_msg.to_json(),
                    properties=pika.BasicProperties(delivery_mode=2)
                )
            logger.info(f"Published {len(messages_to_publish)} packages to {worker_queue}")

        ch.basic_ack(delivery_tag=method.delivery_tag)
        logger.info(f"Successfully finalized epoch {message.epoch_num}")

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
        logger.error(f"Failed to finalize epoch: {e}", exc_info=True)
        # S3 or PostgreSQL unavailable: retried without counting, so an outage
        # delays the epoch step instead of failing the job
        retry_or_fail(ch, method, properties, body, FINISHED_EPOCH_QUEUE,
                      message.job_id if message else None,
                      f"Epoch {message.epoch_num if message else '?'} step", e,
                      count=not is_transient_error(e))


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
                queue=FINISHED_EPOCH_QUEUE,
                auto_ack=False,
                on_message_callback=process_finalize_epoch
            )

            logger.info("Orchestrator ready, waiting for finished epochs...")
            channel.start_consuming()

        except RABBITMQ_CHANNEL_ERRORS as e:
            logger.warning(f"Connection lost: {e}. Reconnecting in {RABBITMQ_RECONNECT_DELAY_SECONDS} seconds...")
            close_quietly(connection)
            time.sleep(RABBITMQ_RECONNECT_DELAY_SECONDS)
        except KeyboardInterrupt:
            logger.info("Shutting down orchestrator...")
            break


if __name__ == '__main__':
    main()
