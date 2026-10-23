# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Watchdog Service for Monolithic Job Recovery (and distributed stall detection)

Monitors jobs and republishes stalled monolithic ones.
Uses database-persisted retry counts to survive restarts.

Distributed (V2) jobs recover a dead merger on their own (takeover), but two
cases need an operator and would otherwise stall silently, IN_PROGRESS forever:
  * the job's Redis state is gone (Redis lost or evicted).
  * no epoch commits any more (e.g. a failover lost an acknowledged commit and
    the fence now rejects every task built on it, or no worker is running).
detect_stalled_v2_jobs() finds both. NBODY_V2_STALL_ACTION=fail (the default)
marks such a job FAILED (Redis, fenced on its generation, then PostgreSQL), so
it shows up and the work queued behind it moves on. "log" only logs. The job can then
be restarted from its latest snapshot with the recovery endpoint.
"""

import os
import sys
import time
from datetime import timedelta, timezone

import pika
from peewee import fn

# MAX_REPUBLISH_ATTEMPTS is shared with the worker: both allow that many retries
# (the worker checks after incrementing retry_count, so > , this before, so >=)
from constants import LEASE_DURATION_SECONDS, MAX_REPUBLISH_ATTEMPTS
from job_timestamps import wait_for_job_schema
from logging_config import setup_logging
from models import Job, JobStatus, JobType, db, utcnow
from rabbitmq import (
    GPU_MONOLITHIC_QUEUE,
    MONOLITHIC_QUEUE,
    RABBITMQ_CONNECTION_ERRORS,
    ensure_rabbitmq_queues,
    get_rabbitmq_connection,
)
from schemas import MonolithicJobMessage
from v2.redis_service import get_redis_service


logger = setup_logging('watchdog')


def ensure_timezone_aware(dt):
    """dt with a timezone (UTC if it is naive). PostgreSQL TIMESTAMP WITH TIME
    ZONE columns may come back naive depending on Peewee configuration and
    existing data, and utcnow() is always aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

# Configuration
STALE_THRESHOLD_MINUTES = 30  # an owned job without progress for this long is hung
ORPHAN_THRESHOLD_MINUTES = 15  # 15 minutes for orphaned job detection
LEASE_GRACE_SECONDS = 30      # past LEASE_DURATION_SECONDS before a lease counts as dead
CHECK_INTERVAL_SECONDS = 30   # takeover follows lease expiry within half a minute

# Distributed (V2) stall detection: no committed epoch for this long, or no
# Redis state at all (after the grace period that covers job creation).
V2_STALL_MINUTES = float(os.environ.get('NBODY_V2_STALL_MINUTES', '30'))
V2_STALL_ACTION = os.environ.get('NBODY_V2_STALL_ACTION', 'fail').strip().lower()
V2_MISSING_STATE_GRACE_MINUTES = 10

# Monolithic job states the watchdog acts on (checked every CHECK_INTERVAL_SECONDS):
# 1. Dead owner: the lease was not renewed for LEASE_DURATION_SECONDS +
#    LEASE_GRACE_SECONDS -> republish (monolithic_stale_reason).
# 2. Hung owner: the lease is renewed but no progress was written for
#    STALE_THRESHOLD_MINUTES (workers write progress at least every 5 min)
#    -> republish.
# 3. Orphaned (epoch 0, no owner, older than ORPHAN_THRESHOLD_MINUTES): its
#    RabbitMQ message may be lost (broker crash). It cannot be recovered
#    automatically, so the watchdog only logs a warning for an operator.


def monolithic_stale_reason(job, now=None):
    """Why a running monolithic job needs republishing, or None.

    A worker acknowledges the job's message when it claims the job and then
    renews the job's lease in the background (worker_monolithic_base.
    LeaseRenewer), so the lease is the only record of a live owner:
      - lease expired: the owner stopped renewing (killed, node lost) or
        handed the job back (expire_job_lease) and its republish failed.
      - hung: the lease is renewed but the job made no progress for
        STALE_THRESHOLD_MINUTES (workers write progress at least every 5 min).
    A job without an owner (worker_id NULL) is waiting in the queue for a
    free worker: never stale here, however long the queue (republishing it
    would only add duplicates).
    """
    if job.status != JobStatus.IN_PROGRESS.value or job.worker_id is None:
        return None
    now = now or utcnow()
    leased_at = ensure_timezone_aware(job.leased_at)
    if leased_at is None or now - leased_at > timedelta(seconds=LEASE_DURATION_SECONDS + LEASE_GRACE_SECONDS):
        age = "never renewed" if leased_at is None else f"renewed {(now - leased_at).total_seconds() / 60:.1f}m ago"
        return f"lease of {job.worker_id} expired ({age})"
    updated_at = ensure_timezone_aware(job.updated_at)
    if updated_at is not None and now - updated_at > timedelta(minutes=STALE_THRESHOLD_MINUTES):
        return (f"no progress for {(now - updated_at).total_seconds() / 60:.1f}m "
                f"while {job.worker_id} holds the lease")
    return None


def detect_stale_jobs():
    """Running monolithic jobs whose owner is dead or hung
    (monolithic_stale_reason)."""
    try:
        owned = Job.select().where(
            (Job.status == JobStatus.IN_PROGRESS.value) &
            (Job.job_type == JobType.MONOLITHIC.value) &
            (Job.worker_id.is_null(False)))
        now = utcnow()
        return [j for j in owned if monolithic_stale_reason(j, now)]
    except Exception as e:
        logger.error(f"Query failed: {e}", exc_info=True)
        return []


def detect_orphaned_jobs():
    """Find jobs that may have lost their RabbitMQ messages.

    Orphaned jobs have:
    - status = IN_PROGRESS
    - current_epoch = 0 (never started)
    - worker_id = NULL (no worker ever claimed it)
    - created_at > 15 minutes ago

    These jobs cannot be automatically recovered by the watchdog because:
    - The RabbitMQ message is lost (broker crashed before persistence)
    - No worker will ever pick them up
    - Require manual intervention (API call to regenerate message)

    This function only logs warnings to alert operators.
    """
    orphan_threshold = utcnow() - timedelta(minutes=ORPHAN_THRESHOLD_MINUTES)

    try:
        orphaned = Job.select().where(
            (Job.status == JobStatus.IN_PROGRESS.value) &
            (Job.job_type == JobType.MONOLITHIC.value) &
            (Job.current_epoch == 0) &
            (Job.worker_id.is_null()) &
            (Job.created_at < orphan_threshold)
        )

        orphaned_list = list(orphaned)

        if len(orphaned_list) > 0:
            logger.warning("=" * 80)
            logger.warning(f"ORPHANED JOBS DETECTED: {len(orphaned_list)}")
            logger.warning("These jobs may have lost their RabbitMQ messages")
            logger.warning("Automatic recovery NOT possible - requires manual intervention")
            logger.warning("=" * 80)

            for job in orphaned_list:
                # Ensure timezone-aware comparison
                created_at = ensure_timezone_aware(job.created_at)
                age_minutes = (utcnow() - created_at).total_seconds() / 60
                logger.warning(f"  - Job {job.job_id}: created {age_minutes:.1f} min ago, never claimed by worker")
                logger.warning(f"    File: {job.filename}, Epochs: {job.num_epochs:,}")
                logger.warning(f"    Solution: Call POST /api/jobs/{job.job_id}/regenerate-message")

            logger.warning("=" * 80)

        return len(orphaned_list)

    except Exception as e:
        logger.error(f"Orphan detection failed: {e}", exc_info=True)
        return 0


def should_republish_job(job: Job) -> bool:
    """Whether a stale job should be republished. The job is read again: its
    worker may have made progress since detect_stale_jobs() queried it."""
    try:
        job_fresh = Job.get(Job.job_id == job.job_id)
    except Exception as e:
        logger.error(f'[JOB {job.job_id}] Failed to reload job: {e}')
        return False

    # Verify still IN_PROGRESS
    if job_fresh.status != JobStatus.IN_PROGRESS.value:
        logger.debug(f'[JOB {job.job_id}] Status changed to {job_fresh.status}')
        return False

    # Verify still stale using FRESH timestamps (a job that recovered is not
    # failed for its past retries)
    reason = monolithic_stale_reason(job_fresh)
    if reason is None:
        logger.debug(f'[JOB {job.job_id}] No longer stale')
        return False

    # Check retry limit (from fresh database read). A job its owner handed
    # back is not failed here: the worker applied its own retry budget (an
    # outage is not counted), and the expired lease only means the hand-back's
    # message has not been consumed yet, or its publish failed.
    if getattr(job_fresh, 'handed_back_at', None) is not None:
        return True
    if job_fresh.retry_count >= MAX_REPUBLISH_ATTEMPTS:
        logger.error(f'[JOB {job.job_id}] Max retries exceeded ({job_fresh.retry_count})')
        try:
            # Only the stale attempt that was read: a worker that finished
            # (or a cancel) in the meantime is not overwritten
            failed = Job.update(
                status=JobStatus.FAILED.value,
                updated_at=utcnow()
            ).where((Job.job_id == job.job_id)
                    & (Job.status == JobStatus.IN_PROGRESS.value)
                    & (Job.worker_id == job_fresh.worker_id)).execute()
            if failed:
                logger.error(f'[JOB {job.job_id}] Marked as FAILED')
        except Exception as e:
            logger.error(f'[JOB {job.job_id}] Failed to update status: {e}')
        return False
    return True


def republish_job(job: Job, channel) -> bool:
    """Republish a stale job and count the retry. The job is read and checked
    again first: its worker may have recovered since should_republish_job()."""

    try:
        try:
            job_fresh = Job.get(Job.job_id == job.job_id)
        except Exception as e:
            logger.error(f'[JOB {job.job_id}] Failed to reload job: {e}')
            return False

        # Verify still IN_PROGRESS
        if job_fresh.status != JobStatus.IN_PROGRESS.value:
            logger.debug(f'[JOB {job.job_id}] Status changed to {job_fresh.status}, skipping republish')
            return False

        # Verify still stale using same logic as should_republish_job()
        reason = monolithic_stale_reason(job_fresh)
        if reason is None:
            logger.info(f'[JOB {job.job_id}] Job recovered before republish')
            return False

        failure_type = "startup" if job_fresh.current_epoch == 0 else "runtime"
        logger.warning(f'[JOB {job_fresh.job_id}] {failure_type.capitalize()} failure detected')

        # Reconstruct the message from the persisted configuration
        message = MonolithicJobMessage.from_job(job_fresh)

        # Publish first, then change the job's state.
        # The reverse order (increment retry_count + release lease, then
        # publish) can strand the job when the publish fails: worker_id and
        # leased_at cannot be restored, and with a NULL lease the job is never
        # seen as stale again.
        # Publish-first failure modes are all recoverable:
        #   - publish fails -> nothing mutated, next iteration retries cleanly
        #   - publish ok, DB update fails -> message is queued. The consumer
        #     can't acquire the still-held lease and acks the duplicate. Next
        #     iteration re-detects staleness and repeats (at-least-once).
        target_queue = GPU_MONOLITHIC_QUEUE if job_fresh.gpu_enabled else MONOLITHIC_QUEUE
        try:
            channel.basic_publish(
                exchange='',
                routing_key=target_queue,
                body=message.to_json(),
                properties=pika.BasicProperties(delivery_mode=2)
            )
        except RABBITMQ_CONNECTION_ERRORS + (pika.exceptions.ChannelWrongStateError,) as publish_error:
            # a dead channel or connection (e.g. the broker restarted under
            # us): let the main loop reconnect instead of failing on the same
            # channel every iteration
            logger.error(f'[JOB {job_fresh.job_id}] Republish failed on a closed channel, reconnecting: '
                         f'{publish_error}')
            raise
        except Exception as publish_error:
            logger.error(f'[JOB {job_fresh.job_id}] Republish publish failed (no state mutated, will retry): '
                         f'{publish_error}')
            return False

        # Increment retry counter and release the lease so the consuming
        # worker can acquire it. Both worker and watchdog increment to
        # ensure the retry limit is enforced. recovery_count (never reset) is
        # NOT bumped here: the worker that takes the republished job over
        # bumps it once (record_recovery_if_resumed), whether the job arrives
        # through this republish or a RabbitMQ redelivery, so a recovery is
        # never counted twice.
        # The lease is released only if the owner seen at the recheck still
        # holds it: once published, a consumer may already have taken the
        # expired lease over, and clearing that new owner's lease would leave
        # the job IN_PROGRESS with nobody recorded as running it (its renewals
        # would then fail). A hung owner keeps renewing leased_at, so the
        # guard is the owner, not the lease time.
        # A job handed back by its owner is republished uncounted (see
        # should_republish_job): the hand-back is not a failure of the job.
        handed_back = getattr(job_fresh, 'handed_back_at', None) is not None
        retries = (fn.COALESCE(Job.retry_count, 0) if handed_back
                   else fn.COALESCE(Job.retry_count, 0) + 1)    # Atomic SQL increment
        try:
            released = Job.update(
                retry_count=retries,
                last_retry_at=utcnow(),
                worker_id=None,  # Release lease so worker can acquire it
                leased_at=None,
                handed_back_at=None,
            ).where(
                (Job.job_id == job_fresh.job_id) & (Job.worker_id == job_fresh.worker_id)
            ).execute()
            if not released:
                logger.info(f'[JOB {job_fresh.job_id}] Republished job already taken over, lease kept')
                if not handed_back:
                    Job.update(
                        retry_count=retries,
                        last_retry_at=utcnow(),
                    ).where(Job.job_id == job_fresh.job_id).execute()
            job_fresh = Job.get(Job.job_id == job_fresh.job_id)
        except Exception as update_error:
            logger.error(
                f'[JOB {job_fresh.job_id}] State update failed after successful publish: {update_error}. '
                f'Message is queued, lease still held so the consumer will skip it and '
                f'the next watchdog iteration will retry.'
            )
            return False

        logger.warning("=" * 80)
        logger.warning(f'[JOB {job_fresh.job_id}]   REPUBLISHED')
        logger.warning(f'[JOB {job_fresh.job_id}] Attempts: {job_fresh.retry_count}/{MAX_REPUBLISH_ATTEMPTS}')
        logger.warning(f'[JOB {job_fresh.job_id}] Reason: {reason}'
                       + (' (handed back by its owner: not counted)' if handed_back else ''))
        logger.warning(f'[JOB {job_fresh.job_id}] Progress: {job_fresh.current_epoch:,}/{job_fresh.num_epochs:,}')
        logger.warning("=" * 80)

        return True

    except RABBITMQ_CONNECTION_ERRORS + (pika.exceptions.ChannelWrongStateError,):
        raise                                   # the main loop reconnects
    except Exception as e:
        logger.error(f'[JOB {job.job_id}] Republish failed: {e}', exc_info=True)
        return False


class V2StallTracker:
    """Last committed epoch of each running V2 job and when it was first seen.

    Kept in the watchdog's memory: a restart only delays detection by one
    stall period, and no extra Redis state is needed.
    """

    def __init__(self):
        self._seen = {}   # job_id -> (committed_epoch, generation, first_seen)

    def stalled_for(self, job_id: int, committed: str, generation: str, now) -> float:
        """Minutes since this job's committed epoch last changed."""
        key = (committed, generation)
        prev = self._seen.get(job_id)
        if prev is None or prev[:2] != key:
            self._seen[job_id] = (committed, generation, now)
            return 0.0
        return (now - prev[2]).total_seconds() / 60.0

    def forget(self, job_id: int) -> None:
        self._seen.pop(job_id, None)

    def forget_except(self, job_ids) -> None:
        for j in list(self._seen):
            if j not in job_ids:
                del self._seen[j]


_v2_tracker = V2StallTracker()


def detect_stalled_v2_jobs(redis_service=None, tracker: V2StallTracker = None, now=None) -> list:
    """Running V2 jobs whose Redis state is gone, or that committed no epoch
    for V2_STALL_MINUTES: (job_id, reason, None) when the Redis state is
    gone, else (job_id, reason, generation, committed_epoch)."""
    tracker = tracker or _v2_tracker
    now = now or utcnow()
    try:
        jobs = list(Job.select().where(
            (Job.status == JobStatus.IN_PROGRESS.value) &
            (Job.job_type == JobType.DISTRIBUTED_V2.value)
        ))
    except Exception as e:
        logger.error(f"V2 query failed: {e}", exc_info=True)
        return []
    if redis_service is None:
        redis_service = get_redis_service()

    stalled = []
    for job in jobs:
        meta = redis_service.get_job_meta(job.job_id)
        if not meta or 'status' not in meta:
            created = ensure_timezone_aware(job.updated_at or job.created_at)
            if created and now - created > timedelta(minutes=V2_MISSING_STATE_GRACE_MINUTES):
                stalled.append((job.job_id, "Redis state lost (no job meta)", None))
            continue
        if meta.get('status') != 'IN_PROGRESS':
            # paused, or final in Redis while PostgreSQL lags: not a stall. A
            # resumed job starts a fresh stall clock
            tracker.forget(job.job_id)
            continue
        minutes = tracker.stalled_for(job.job_id, meta.get('committed_epoch', meta.get('current_epoch', '')),
                                      meta.get('generation', '0'), now)
        if minutes >= V2_STALL_MINUTES:
            stalled.append((job.job_id,
                            f"no epoch committed for {minutes:.0f} min "
                            f"(committed epoch {meta.get('committed_epoch')})",
                            meta.get('generation', '0'),
                            meta.get('committed_epoch')))
    tracker.forget_except({j.job_id for j in jobs})
    return stalled


def handle_stalled_v2_jobs(stalled, redis_service=None) -> int:
    """Log each stalled V2 job. With NBODY_V2_STALL_ACTION=fail also mark it
    FAILED (Redis fenced on the generation seen, then PostgreSQL if it is still
    IN_PROGRESS). Returns the number of jobs marked."""
    marked = 0
    for job_id, reason, generation, *seen in stalled:
        committed = seen[0] if seen else None   # epoch observed stalled. Absent for lost state
        hint = f"recover with POST /api/v2/jobs/{job_id}/recover"
        if V2_STALL_ACTION != 'fail':
            logger.warning(f"V2 job {job_id} stalled: {reason}, {hint}")
            continue
        try:
            if generation is not None:
                if redis_service is None:
                    redis_service = get_redis_service()
                if redis_service.fail_job(job_id, f"watchdog: {reason}", int(generation),
                                          None if committed is None else int(committed)) < 0:
                    continue      # it moved on (committed, recovered, cancelled) meanwhile
            updated = Job.update(status=JobStatus.FAILED.value, updated_at=utcnow()).where(
                (Job.job_id == job_id) & (Job.status == JobStatus.IN_PROGRESS.value)
            ).execute()
            if updated:
                marked += 1
                logger.error(f"V2 job {job_id} marked FAILED: {reason}, {hint}")
        except Exception as e:  # noqa: BLE001 — try again next iteration
            logger.error(f"Could not mark stalled V2 job {job_id}: {e}")
    return marked


def reconcile_terminal_v2_jobs(redis_service=None) -> int:
    """Copy a terminal Redis status onto a PostgreSQL row that still says the
    job is running. A worker records FAILED (or the API CANCELLED) in
    Redis first. If PostgreSQL is down at that moment its row stays
    IN_PROGRESS for good, because later tasks of the job are dropped once
    Redis is terminal. Completion retries through its pending epoch, but is
    reconciled here too. The write is conditional on the row's updated_at
    being the one read before Redis was: recovery restores Redis first and
    then writes the row (updated_at included), so a recovery after the Redis
    read makes this write match nothing, and one before it is seen in Redis.
    (Re-reading Redis alone would only narrow that window.)
    Returns the number of rows updated."""
    active = (JobStatus.PENDING.value, JobStatus.IN_PROGRESS.value, JobStatus.PAUSED.value)
    try:
        jobs = list(Job.select().where(
            (Job.status.in_(active)) & (Job.job_type == JobType.DISTRIBUTED_V2.value)))
    except Exception as e:  # noqa: BLE001
        logger.error(f"V2 reconcile query failed: {e}")
        return 0
    if not jobs:
        return 0
    if redis_service is None:
        redis_service = get_redis_service()
    terminal = (JobStatus.FAILED.value, JobStatus.CANCELLED.value, JobStatus.COMPLETED.value)
    updated = 0
    for job in jobs:
        try:
            meta = redis_service.get_job_meta(job.job_id) or {}
            status = meta.get('status')
            if status not in terminal:
                continue
            again = redis_service.get_job_meta(job.job_id) or {}
            if again.get('status') != status or again.get('generation') != meta.get('generation'):
                continue                                  # changed meanwhile (e.g. a recovery)
            fields = dict(status=status, updated_at=utcnow())
            if status == JobStatus.COMPLETED.value:
                fields.update(results_file_key=meta.get('results_file_key', ''), completed_at=utcnow())
                if meta.get('committed_epoch') is not None:
                    fields['current_epoch'] = int(meta['committed_epoch'])
            seen = (Job.updated_at.is_null() if job.updated_at is None
                    else Job.updated_at == job.updated_at)          # unchanged since the select
            n = Job.update(**fields).where((Job.job_id == job.job_id) & (Job.status.in_(active))
                                           & seen).execute()
            if n:
                updated += 1
                logger.warning(f"V2 job {job.job_id}: PostgreSQL said {job.status}, Redis {status}, "
                               f"reconciled to {status}")
        except Exception as e:  # noqa: BLE001 — try again next iteration
            logger.error(f"Could not reconcile V2 job {job.job_id}: {e}")
    return updated


def watchdog_iteration(channel) -> int:
    """Single watchdog check iteration."""

    try:
        try:
            reconcile_terminal_v2_jobs()
            handle_stalled_v2_jobs(detect_stalled_v2_jobs())
        except Exception as e:  # noqa: BLE001 — never block monolithic recovery
            logger.error(f"V2 stall check failed: {e}", exc_info=True)

        stale_jobs = detect_stale_jobs()

        if not stale_jobs:
            logger.debug("No stale jobs")
        else:
            logger.info(f"Found {len(stale_jobs)} potentially stale job(s)")

        republished = 0
        for job in stale_jobs:
            if should_republish_job(job):
                if republish_job(job, channel):
                    republished += 1

        if republished > 0:
            logger.info(f"Republished {republished} job(s)")

        # Orphaned jobs are only logged for an operator (see detect_orphaned_jobs)
        detect_orphaned_jobs()

        return republished

    except RABBITMQ_CONNECTION_ERRORS + (pika.exceptions.ChannelWrongStateError,):
        raise                                   # the main loop reconnects
    except Exception as e:
        logger.error(f"Iteration failed: {e}", exc_info=True)
        return 0


def connect_rabbitmq():
    """Establish RabbitMQ connection with retry logic."""
    logger.info("Connecting to RabbitMQ...")
    connection = get_rabbitmq_connection()
    channel = connection.channel()
    ensure_rabbitmq_queues(channel)
    logger.info("RabbitMQ connected")
    return connection, channel


def main():
    """Main watchdog loop."""
    # Wait for the API's schema migration (new model columns) before querying jobs
    wait_for_job_schema()

    logger.info("=" * 80)
    logger.info("Watchdog Service Starting")
    logger.info("Config:")
    logger.info(f"  - Monolithic: republish at lease expiry ({LEASE_DURATION_SECONDS}s + {LEASE_GRACE_SECONDS}s) "
                f"or after {STALE_THRESHOLD_MINUTES} min without progress")
    logger.info(f"  - Orphan detection threshold: {ORPHAN_THRESHOLD_MINUTES} minutes")
    logger.info(f"  - Check interval: {CHECK_INTERVAL_SECONDS} s")
    logger.info(f"  - Distributed stall: {V2_STALL_MINUTES:.0f} minutes without a committed epoch "
                f"(action: {V2_STALL_ACTION})")
    logger.info(f"  - Max retry attempts: {MAX_REPUBLISH_ATTEMPTS}")
    logger.info("=" * 80)

    # Initial RabbitMQ connection
    connection = None
    channel = None
    try:
        connection, channel = connect_rabbitmq()
    except Exception as e:
        logger.critical(f"Initial RabbitMQ connection failed: {e}", exc_info=True)
        sys.exit(1)

    # Connect to database
    logger.info("Connecting to database...")
    try:
        db.connect(reuse_if_open=True)
        logger.info("Database connected")
    except Exception as e:
        logger.critical(f"Database connection failed: {e}", exc_info=True)
        sys.exit(1)

    logger.info("Watchdog running")
    logger.info("=" * 80)

    iteration = 0
    while True:
        try:
            iteration += 1
            logger.debug(f"🔍 Check #{iteration}")

            # A broker restart leaves the connection or channel closed without
            # any error reaching this loop. Check before using them.
            if connection is None or not connection.is_open or channel is None or not channel.is_open:
                logger.warning("RabbitMQ connection or channel closed, reconnecting")
                connection, channel = connect_rabbitmq()

            republished = watchdog_iteration(channel)

            if republished == 0:
                logger.debug(f"Check #{iteration} complete: no actions")
            else:
                logger.info(f"Check #{iteration} complete: {republished} republished")

        except KeyboardInterrupt:
            logger.info("Shutdown requested")
            break

        except RABBITMQ_CONNECTION_ERRORS + (pika.exceptions.ChannelWrongStateError,) as e:
            logger.warning(f"RabbitMQ connection lost: {e}. Reconnecting...")
            try:
                connection, channel = connect_rabbitmq()
            except Exception as reconnect_error:
                logger.error(f"Reconnection failed: {reconnect_error}. Will retry next iteration.")

        except Exception as e:
            logger.error(f"Main loop error: {e}", exc_info=True)

        # Sleep BETWEEN checks via the pika connection so heartbeat frames are
        # serviced during the wait. A plain time.sleep(CHECK_INTERVAL_SECONDS)
        # blocks the BlockingConnection for as long as the heartbeat timeout
        # (RABBITMQ_HEARTBEAT, default 60s) or longer, so the broker drops the connection and
        # the watchdog spends every cycle reconnecting (and can silently fail to
        # republish). connection.sleep() pumps I/O while waiting.
        try:
            if connection is not None and connection.is_open:
                connection.sleep(CHECK_INTERVAL_SECONDS)
            else:
                time.sleep(CHECK_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            break
        except RABBITMQ_CONNECTION_ERRORS as e:
            # Connection dropped mid-sleep. Reconnect on the next iteration. Guard
            # against a busy loop with a short plain sleep.
            logger.warning(f"RabbitMQ connection lost during sleep: {e}. Reconnecting next cycle.")
            time.sleep(min(CHECK_INTERVAL_SECONDS, 5))

    # Cleanup
    logger.info("Closing connections...")
    try:
        if connection and connection.is_open:
            connection.close()
        if db and not db.is_closed():
            db.close()
        logger.info("Cleanup complete")
    except Exception as e:
        logger.error(f"Cleanup error: {e}")

    logger.info("🐕 Watchdog stopped")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logger.critical(f"Fatal: {e}", exc_info=True)
        sys.exit(1)
