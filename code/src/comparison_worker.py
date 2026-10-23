# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Consumes comparison job messages from RabbitMQ, computes full
job-to-job comparisons using the same logic as the ad-hoc endpoint,
and stores results in S3 for persistent retrieval.
"""

import asyncio
import io
import json
import threading
import time

import pika

from async_file_service import EPOCH_RESULTS_BUCKET_NAME
from constants import RABBITMQ_RECONNECT_DELAY_SECONDS
from db_threads import close_thread_connection
from file_service import upload_file, verify_file_exists
from job_timestamps import wait_for_job_schema
from logging_config import setup_logging
from models import ComparisonJob, Job, utcnow
from rabbitmq import COMPARISON_QUEUE, RABBITMQ_CONNECTION_ERRORS, ConnectionThreadChannel, get_rabbitmq_connection
from schemas import ComparisonMessage, JobStatus
from services.comparison_cache import _persistent_metrics_key, store_persistent_metrics
from services.comparison_service import compute_full_comparison, compute_job_metrics_streaming
from transient_errors import is_transient_error


logger = setup_logging('comparison_worker')

# Transient failures are re-queued (with this cap) instead of permanently
# marking the comparison FAILED — an S3/DB blip shouldn't lose the work.
MAX_TRANSIENT_RETRIES = 3

# Attempts the worker may die during (killed for memory, node lost) before
# the comparison is marked FAILED. Without a cap the message came back at the
# head of the queue forever and, with prefetch 1, blocked every comparison
# behind it.
MAX_CRASHED_ATTEMPTS = 2


def _is_transient_error(exc: Exception) -> bool:
    """Whether an error is infrastructure-related and worth retrying.

    Data errors (missing jobs, malformed snapshots, value errors) are
    permanent — retrying reproduces them. Connectivity/availability errors
    to S3, PostgreSQL or the network are transient (transient_errors, as for
    the workers), and so are socket-level OSErrors (ConnectionResetError,
    BrokenPipeError, ...).
    """
    return is_transient_error(exc) or isinstance(exc, OSError)


def _numpy_safe_serializer(obj):
    """JSON serializer that handles numpy types."""
    if hasattr(obj, 'item'):
        return obj.item()  # numpy scalar → Python scalar
    if hasattr(obj, 'tolist'):
        return obj.tolist()  # numpy array → list
    return str(obj)


def _get_job(job_id: int) -> Job:
    """The job, or LookupError when it was deleted after the comparison was
    submitted (the message is stored as the comparison's error)."""
    try:
        return Job.get(Job.job_id == job_id)
    except Job.DoesNotExist:
        raise LookupError(f"Job {job_id} was deleted") from None


def process_comparison(ch, method, properties, body):
    """Process a single comparison job message."""
    try:
        message = ComparisonMessage.from_json(body)
    except Exception as e:  # noqa: BLE001 — malformed: no redelivery can parse it
        # Raising here would stop the consumer, and the broker would hand the
        # same message to the restarted worker forever.
        logger.error(f"Dropping a comparison message that could not be parsed: {e}")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return
    logger.info(f"Processing comparison {message.comparison_id}: "
                f"ref={message.reference_job_id}, "
                f"cmp={message.comparison_job_ids}")
    headers = dict(getattr(properties, 'headers', None) or {})

    try:
        comparison = ComparisonJob.get(
            ComparisonJob.comparison_id == message.comparison_id
        )
    except ComparisonJob.DoesNotExist:
        logger.error(f"Comparison {message.comparison_id} not found in DB, skipping")
        _settle(ch.basic_ack, method.delivery_tag)
        return

    try:
        # Idempotency check: if already COMPLETED or FAILED, skip
        if comparison.status in ('COMPLETED', 'COMPLETED_EMPTY', 'FAILED'):
            logger.info(f"Comparison {message.comparison_id} already {comparison.status}, skipping")
            _settle(ch.basic_ack, method.delivery_tag)
            return

        if comparison.status == 'COMPUTING' and (method.redelivered or not headers.get('x-requeued')):
            # an earlier attempt never finished: the worker died during it.
            # (A fresh copy from the transient requeue below can find
            # COMPUTING too, when resetting the status failed with the outage
            # that caused it: that is not a crash.)
            crashes = int(headers.get('x-crashes', 0)) + 1
            if crashes > MAX_CRASHED_ATTEMPTS:
                logger.error(f"Comparison {message.comparison_id}: the worker stopped during "
                             f"{crashes} attempts, marking it FAILED")
                comparison.status = 'FAILED'
                comparison.error_message = (f"The comparison worker stopped during {crashes} attempts "
                                            f"(out of memory?), try fewer jobs or max_snapshots")
                comparison.updated_at = utcnow()
                comparison.save()
                _settle(ch.basic_ack, method.delivery_tag)
                return
            logger.warning(f"Comparison {message.comparison_id} was interrupted (attempt {crashes} of "
                           f"{MAX_CRASHED_ATTEMPTS}), requeueing it at the back of the queue")
            comparison.status = 'PENDING'
            comparison.updated_at = utcnow()
            comparison.save()
            _requeue(ch, method, body, {**headers, 'x-crashes': crashes, 'x-requeued': 0})
            return

        comparison.status = 'COMPUTING'
        comparison.started_at = utcnow()
        comparison.updated_at = utcnow()
        comparison.save()

        start = time.time()

        ref_job = _get_job(message.reference_job_id)
        cmp_job_ids = json.loads(message.comparison_job_ids)
        cmp_jobs = [_get_job(jid) for jid in cmp_job_ids]

        # Run the comparison (reuses exact same logic as ad-hoc endpoint)
        results = asyncio.run(
            compute_full_comparison(ref_job, cmp_jobs, message.max_snapshots)
        )

        elapsed_ms = int((time.time() - start) * 1000)

        # Serialize results to S3. Namespace under job_id/comparison/{id}/ so a
        # comparison never shares the job_id/{id}/ prefix with a simulation job of
        # the same numeric id — otherwise a permanent job delete (which prefix-
        # deletes job_id/{job_id}/) could collaterally wipe this comparison's
        # results, and vice versa. The full key is persisted as results_file_key,
        # so reads/deletes of existing (old-prefix) comparisons keep working.
        results_key = f'job_id/comparison/{message.comparison_id}/comparison_results.json'
        results_json = json.dumps(results, default=_numpy_safe_serializer)
        buffer = io.BytesIO(results_json.encode('utf-8'))
        upload_file(EPOCH_RESULTS_BUCKET_NAME, results_key, buffer)

        # Determine final status
        comparisons_count = len(results.get('comparisons', []))
        skipped_count = len(results.get('skipped_jobs', []))
        final_status = 'COMPLETED_EMPTY' if comparisons_count == 0 else 'COMPLETED'

        comparison.status = final_status
        comparison.results_file_key = results_key
        comparison.computation_time_ms = elapsed_ms
        comparison.jobs_compared = comparisons_count
        comparison.jobs_skipped = skipped_count
        comparison.updated_at = utcnow()
        comparison.save()

        logger.info(f"Comparison {message.comparison_id} completed: "
                    f"{comparisons_count} compared, {skipped_count} skipped, "
                    f"{elapsed_ms}ms")

    except Exception as e:
        # Transient infrastructure errors: re-queue (capped) instead of
        # permanently failing the comparison over an S3/DB blip.
        attempts = int(headers.get('x-retries', 0))
        if _is_transient_error(e) and attempts < MAX_TRANSIENT_RETRIES:
            logger.warning(
                f"Comparison {message.comparison_id} hit transient error "
                f"(attempt {attempts + 1}/{MAX_TRANSIENT_RETRIES}), re-queuing: {e}"
            )
            try:
                # Reset status so the idempotency check doesn't skip the retry
                comparison.status = 'PENDING'
                comparison.updated_at = utcnow()
                comparison.save()
            except Exception:
                # DB itself may be the transient failure — the COMPUTING
                # redelivery path retries those anyway
                pass
            _requeue(ch, method, body, {**headers, 'x-retries': attempts + 1, 'x-requeued': 1})
            return

        logger.error(f"Comparison {message.comparison_id} failed permanently: {e}", exc_info=True)
        try:
            comparison.status = 'FAILED'
            comparison.error_message = str(e)[:500]
            comparison.updated_at = utcnow()
            comparison.save()
        except Exception as db_error:
            logger.error(f"Could not mark comparison {message.comparison_id} FAILED: {db_error}")
    # Settled outside the try: a broker connection lost while the comparison
    # ran makes the ack fail, and handling that as the comparison's error
    # overwrote its stored COMPLETED with FAILED. Unacknowledged, the message
    # is redelivered and found done.
    _settle(ch.basic_ack, method.delivery_tag)


def _settle(ack, delivery_tag) -> None:
    """Acknowledge a delivery. With the connection gone, leave it to the redelivery."""
    try:
        ack(delivery_tag=delivery_tag)
    except Exception as e:  # noqa: BLE001 — closed channel/connection
        logger.warning(f"Could not acknowledge delivery {delivery_tag} (broker connection lost?), "
                       f"it is redelivered: {e}")


def _requeue(ch, method, body, headers: dict) -> None:
    """Publish the message again at the back of the queue, then acknowledge
    this delivery (a requeue by nack would put it back at the head)."""
    try:
        ch.basic_publish(
            exchange='',
            routing_key=COMPARISON_QUEUE,
            body=body,
            properties=pika.BasicProperties(delivery_mode=2, headers=headers),
        )
    except Exception as e:  # noqa: BLE001 — unpublished: keep this delivery for the broker to redeliver
        logger.warning(f"Could not requeue delivery {method.delivery_tag}: {e}")
        return
    _settle(ch.basic_ack, method.delivery_tag)


# Metrics precompute reconciliation

PRECOMPUTE_POLL_SECONDS = 60
PRECOMPUTE_MAX_FAILURES = 3


def precompute_job_metrics(job) -> None:
    """Compute and persist conservation metrics for one completed job.

    Identical computation and storage to the dashboard's
    /api/comparison/jobs/{id}/metrics endpoint, so a later view is a pure
    cache hit.
    """

    async def _run():
        result = await compute_job_metrics_streaming(job, max_snapshots=None)
        await store_persistent_metrics(job.job_id, None, result)

    asyncio.run(_run())


def metrics_precompute_loop():
    """Reconciliation loop: every COMPLETED job ends up with persisted metrics.

    One code path covers freshly-completed jobs (picked up within one poll
    interval, so the dashboard's first view is a cache hit) AND the backfill
    of all historical jobs — no completion hooks needed in the monolithic
    worker, orchestrator, or V2 finalize (which has no RabbitMQ access).

    State is in-memory only: on restart the loop re-verifies via cheap S3
    HEAD requests and converges again.
    """

    verified = set()
    failures = {}

    logger.info("Metrics precompute loop started")
    while True:
        try:
            completed_jobs = (
                Job.select()
                .where(Job.status == JobStatus.COMPLETED.value)
                .order_by(Job.updated_at.desc())
            )
            for job in completed_jobs:
                if job.job_id in verified:
                    continue
                if failures.get(job.job_id, 0) >= PRECOMPUTE_MAX_FAILURES:
                    continue

                key = _persistent_metrics_key(job.job_id, None)
                if verify_file_exists(EPOCH_RESULTS_BUCKET_NAME, key):
                    verified.add(job.job_id)
                    continue
                if not job.results_file_key:
                    # Nothing to compute from (results never landed)
                    verified.add(job.job_id)
                    continue

                logger.info(f"Precomputing metrics for completed job {job.job_id}")
                try:
                    t0 = time.time()
                    precompute_job_metrics(job)
                    verified.add(job.job_id)
                    logger.info(
                        f"Precomputed metrics for job {job.job_id} in {time.time() - t0:.1f}s"
                    )
                except Exception as e:
                    failures[job.job_id] = failures.get(job.job_id, 0) + 1
                    logger.warning(
                        f"Metrics precompute failed for job {job.job_id} "
                        f"(attempt {failures[job.job_id]}/{PRECOMPUTE_MAX_FAILURES}): {e}"
                    )
        except Exception as e:
            logger.error(f"Metrics precompute sweep failed: {e}", exc_info=True)

        time.sleep(PRECOMPUTE_POLL_SECONDS)


# The thread running the current comparison (one at a time, see _on_message)
_comparison_thread: threading.Thread = None

# Wait before requeueing a message whose handling failed outside
# process_comparison's own error handling (e.g. the database is unreachable)
UNHANDLED_RETRY_DELAY_S = 5


def _run_comparison(ch, method, properties, body):
    """Thread target: process one message. Never die without settling it,
    and return the thread's pooled connection (the pool reclaims none)."""
    try:
        process_comparison(ch, method, properties, body)
    except Exception as e:  # noqa: BLE001 — e.g. the database is down at ComparisonJob.get
        logger.error(f"Comparison message failed outside its handler, requeueing: {e}", exc_info=True)
        time.sleep(UNHANDLED_RETRY_DELAY_S)
        try:
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        except Exception:  # noqa: BLE001 — connection gone: the broker redelivers anyway
            pass
    finally:
        close_thread_connection()


def _on_message(ch, method, properties, body):
    """Consumer callback: the comparison runs in a thread, so the connection
    thread keeps answering heartbeats however long it takes (a comparison
    run inside the callback lost its connection after the heartbeat timeout
    and was recomputed on every redelivery). prefetch_count=1 holds the next
    delivery back until this one is acknowledged."""
    global _comparison_thread
    _comparison_thread = threading.Thread(
        target=_run_comparison, args=(ConnectionThreadChannel(ch), method, properties, body),
        name='comparison', daemon=True)
    _comparison_thread.start()


def main():
    """Start the comparison worker. Reconnects whenever the broker drops."""
    logger.info("Starting comparison worker...")
    # Wait for the API's schema migration (new model columns) before consuming
    wait_for_job_schema()

    threading.Thread(
        target=metrics_precompute_loop, daemon=True, name='metrics-precompute'
    ).start()

    while True:
        connection = None
        if _comparison_thread is not None and _comparison_thread.is_alive():
            # a comparison from before the reconnect is still running: its
            # message comes back with the new connection, so don't start a
            # second copy alongside it (prefetch_count bounds one connection)
            logger.info("Waiting for the running comparison before reconnecting")
            _comparison_thread.join()
        try:
            connection = get_rabbitmq_connection()
            channel = connection.channel()
            channel.queue_declare(queue=COMPARISON_QUEUE, durable=True)
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue=COMPARISON_QUEUE, on_message_callback=_on_message)
            logger.info(f"Listening on queue: {COMPARISON_QUEUE}")
            channel.start_consuming()
        except RABBITMQ_CONNECTION_ERRORS as e:
            # an unacknowledged comparison is redelivered after the reconnect
            logger.warning(f"Connection lost: {e}. Reconnecting in {RABBITMQ_RECONNECT_DELAY_SECONDS} seconds...")
            time.sleep(RABBITMQ_RECONNECT_DELAY_SECONDS)
        except KeyboardInterrupt:
            logger.info("Shutting down comparison worker...")
            break
        finally:
            if connection is not None and connection.is_open:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001 — closing a broken connection
                    pass


if __name__ == '__main__':
    main()
