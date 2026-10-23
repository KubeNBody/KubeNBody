# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Monolithic worker: runs a whole job on one worker, with a CPU or GPU
ComputeBackend, taking jobs from RabbitMQ (entry points worker_monolithic.py
and worker_monolithic_gpu.py). A job lease lets another worker take over a
crashed one. A job resumes from its latest snapshot. A graceful shutdown keeps
the progress made."""

import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pika
import pika.exceptions

import task_timing
import worker_metrics
from config import settings
from constants import (
    G_CONST,
    LEASE_RENEWAL_INTERVAL_SECONDS,
    MAX_REPUBLISH_ATTEMPTS,
    PROGRESS_LOG_INTERVAL,
    SOFTENING_SQ,
)
from diagnostics import com_drift, is_finite_state, total_energy
from integrators import is_velocity_verlet
from job_timestamps import mark_job_started, record_compute_hosts
from logging_config import setup_logging
from models import Job, db, utcnow
from physics import estimate_adaptive_dt
from rabbitmq import (
    GPU_MONOLITHIC_QUEUE,
    MONOLITHIC_QUEUE,
    RABBITMQ_CHANNEL_ERRORS,
    RETRY_DELAY_MS,
    close_quietly,
    declare_retry_queue,
    get_rabbitmq_connection,
    handback_headers,
    handback_marker,
    publish_later,
    retry_later,
)
from rabbitmq import ConnectionThreadChannel as _ConnectionThreadChannel
from schemas import JobStatus, MonolithicJobMessage
from transient_errors import is_transient_error
from utils.metadata import compute_host
from worker_monolithic_base import (
    JobCancelledException,
    JobPausedException,
    LeaseLostException,
    LeaseRenewer,
    ShutdownException,
    ShutdownHandler,
    acquire_job_lease,
    expire_job_lease,
    final_snapshot_is_durable,
    finalize_job,
    generate_worker_id,
    get_snapshot_uploader,
    load_bodies_for_job,
    record_recovery_if_resumed,
    release_job_lease,
    renew_job_lease,
    settle_snapshot_uploads,
    should_save_snapshot,
    upload_snapshot_from_arrays,
    upload_snapshot_metadata,
    wait_out_dead_lease,
)
from worker_utils import bodies_to_numpy, bodies_to_numpy_with_accelerations
from workers.base.compute_backend import ComputeBackend, ComputeError, exit_for_restart


PROGRESS_MAX_INTERVAL_S = 300
RECONNECT_DELAY_S = 5
# A job failed by an infrastructure outage (S3, PostgreSQL) comes back after
# this delay, uncounted: an outage of minutes then costs a few hand-backs, not
# the retry budget. Kept well inside the watchdog's grace period (see
# expire_job_lease(delayed=True)).
TRANSIENT_HANDBACK_DELAY_MS = min(RETRY_DELAY_MS, 30_000)
# A failure is not written over a job that finished or was cancelled meanwhile
_FINAL = (JobStatus.COMPLETED.value, JobStatus.CANCELLED.value)


logger = setup_logging('monolithic-worker')


class _JobChannel:
    """The channel as the job thread sees it, across reconnects of the
    connection thread (run()).

    Acks and nacks settle the delivery on the channel it came on. Once that
    connection is gone the broker has requeued the message already, and its
    delivery tag means nothing on a new channel (an unknown tag closes the
    channel), so they are dropped. Publishes, stop requests and callbacks go
    to the worker's current connection. Between connections a publish raises
    (_hand_back then leaves the job to the watchdog) and a callback waits for
    the loop to reconnect.
    """

    def __init__(self, worker, channel):
        self._worker = worker
        self._origin = _ConnectionThreadChannel(channel)
        self.connection = self._origin.connection      # sleep is time.sleep

    def _settle(self, name, kwargs):
        try:
            getattr(self._origin, name)(**kwargs)
        except RABBITMQ_CHANNEL_ERRORS as e:
            logger.warning(f"{name} of delivery {kwargs.get('delivery_tag')} dropped: its connection "
                           f"is gone ({e!r}) and the broker has requeued the message")

    def basic_ack(self, **kwargs):
        self._settle('basic_ack', kwargs)

    def basic_nack(self, **kwargs):
        self._settle('basic_nack', kwargs)

    def _current(self):
        current = self._worker._thread_channel
        if current is None:
            raise pika.exceptions.ConnectionWrongStateError("reconnecting to RabbitMQ")
        return current

    def basic_publish(self, **kwargs):
        self._current().basic_publish(**kwargs)

    def stop_consuming(self):
        try:
            self._current().stop_consuming()
        except RABBITMQ_CHANNEL_ERRORS:
            pass                # the loop stops through _after_job anyway

    def on_connection_thread(self, fn):
        target = None
        try:
            target = self._current()
            target.on_connection_thread(fn)
        except RABBITMQ_CHANNEL_ERRORS:
            pass
        if target is None or self._worker._thread_channel is not target:
            # no connection, or it was replaced meanwhile (the callback may be
            # lost with it): the loop runs it once connected. _after_job is
            # idempotent, so running it twice is harmless
            self._worker._pending_callbacks.put(fn)


@dataclass
class MonolithicJobConfig:
    """Configuration for a monolithic job."""
    job_id: int
    bucket: str
    file_key: str
    num_epochs: int
    time_step: float
    snapshot_interval: int
    algorithm: str = 'direct'
    barnes_hut_theta: float = 0.5
    softening_sq: float = SOFTENING_SQ
    integration_method: str = 'symplectic_euler'
    gpu_enabled: bool = False
    adaptive_enabled: bool = False
    adaptive_eta: float = 0.02
    adaptive_dt_min: float = 1e-10
    adaptive_dt_max: float = 0.0  # 0 means use time_step

    @classmethod
    def from_message(cls, message: MonolithicJobMessage) -> 'MonolithicJobConfig':
        """The job's configuration from its queue message. A dt bound of 0 is
        the default: 1e-10 below, the job's time step above."""
        return cls(
            job_id=message.job_id,
            bucket=message.bucket,
            file_key=message.file_key,
            num_epochs=message.num_of_epochs,
            time_step=message.time_step,
            snapshot_interval=message.snapshot_interval,
            algorithm=message.force_algorithm,
            barnes_hut_theta=message.barnes_hut_theta,
            softening_sq=message.softening_sq,
            integration_method=message.integration_method,
            gpu_enabled=message.gpu_enabled,
            adaptive_enabled=message.adaptive_enabled,
            adaptive_eta=message.adaptive_eta,
            adaptive_dt_min=message.adaptive_dt_min if message.adaptive_dt_min > 0 else 1e-10,
            adaptive_dt_max=message.adaptive_dt_max if message.adaptive_dt_max > 0 else message.time_step,
        )


@dataclass
class _RunState:
    """A monolithic job's state on the host, advanced by the epoch loop."""
    positions: np.ndarray
    velocities: np.ndarray
    masses: np.ndarray
    ids: np.ndarray
    accelerations: Optional[np.ndarray]
    ns: np.ndarray            # the bodies' 'n' values: snapshot rows are written sorted by n
    dt: float
    simulation_time: float


class MonolithicWorker:
    """Unified monolithic worker using pluggable compute backend."""

    def __init__(
        self,
        compute_backend: ComputeBackend,
        worker_id: Optional[str] = None,
    ):
        """A worker running whole jobs on compute_backend (CPU or GPU). worker_id
        is generated if not given."""
        self.compute = compute_backend
        self.worker_id = worker_id or generate_worker_id(compute_backend.name.upper())

        self.shutdown_handler = ShutdownHandler(self.worker_id, logger, exit_when_idle=True)

        # RabbitMQ channel reference (for graceful shutdown)
        self._channel = None

        # Run each job in a thread so the connection thread keeps answering
        # AMQP heartbeats. A job in the consumer callback would block them,
        # needing a heartbeat as long as the longest job and leaving a job
        # whose node was lost undelivered for that long. The GPU backend keeps
        # the synchronous callback: pycuda.autoinit binds its CUDA context to
        # the main thread.
        self._threaded = self.compute.name.lower() != 'gpu'
        self._job_thread = None
        # the queue consumed from: a job handed back after its message was
        # acknowledged is republished there
        self._queue_name = None
        self._lease_renewer = None
        # flow control: the consumer is cancelled while a job runs (see _on_message)
        self._raw_channel = None
        self._consumer_tag = None
        self._stop_loop = False
        # the current connection as the job thread reaches it (None while
        # reconnecting), and callbacks that waited for a connection
        self._thread_channel = None
        self._pending_callbacks = queue.SimpleQueue()
        self._last_heartbeat_service = 0.0

        logger.info(f"MonolithicWorker initialized: {self.worker_id}")
        logger.info(f"  Compute backend: {self.compute.name}")

    def run(self, queue_name: str) -> None:
        """The worker's main loop on the RabbitMQ queue queue_name."""
        logger.info(f"Warming up {self.compute.name} backend...")
        try:
            self.compute.warmup()
            logger.info(f"{self.compute.name} backend warmed up")
        except ComputeError as e:
            logger.error(f"Compute backend warmup failed: {e}")
            if not e.recoverable:
                raise

        logger.info(f"Worker {self.worker_id} ready, consuming from '{queue_name}'...")
        self._queue_name = queue_name
        logger.info(f"Jobs run {'in a job thread' if self._threaded else 'in the consumer callback'}, "
                    f"AMQP heartbeat {settings.rabbitmq.heartbeat} s")

        connection = None
        try:
            # An explicit loop, not channel.start_consuming(): the consumer is
            # cancelled while a job runs, and start_consuming returns as soon as
            # no consumer is left. The loop keeps the connection serviced
            # (heartbeats, the job thread's callbacks) until a stop.
            # A lost connection is replaced while a job thread keeps running:
            # its message was acknowledged at the claim, and it needs the
            # broker only to hand the job back or to take the next one.
            while not self._stop_loop:
                try:
                    if connection is None:
                        connection = self._connect()
                        logger.info(f"Waiting for jobs on '{queue_name}'...")
                    self._run_pending_callbacks()
                    connection.process_data_events(time_limit=1)
                except RABBITMQ_CHANNEL_ERRORS as e:
                    running = self._job_thread is not None and self._job_thread.is_alive()
                    logger.warning(f"Lost the RabbitMQ connection ({e!r}), reconnecting"
                                   + (" while the job keeps running" if running else ""))
                    self._disconnect(connection)
                    connection = None
        except KeyboardInterrupt:
            logger.info("Shutting down gracefully...")
        finally:
            # let a job thread that stopped the loop finish its cleanup first
            if self._job_thread is not None and self._job_thread.is_alive():
                self._job_thread.join(timeout=60)
            close_quietly(connection)
            self.compute.cleanup()
            logger.info("Worker shutdown complete")

    def _connect(self):
        """Open the connection and channel and consume again, unless a job is
        running (its end re-registers the consumer) or a stop was requested.
        With no job running a broker that stays unreachable ends the worker,
        as at startup. While one runs, connecting is retried."""
        while True:
            connection = None
            try:
                connection = get_rabbitmq_connection()
                channel = connection.channel()
                channel.queue_declare(queue=self._queue_name, durable=True)
                declare_retry_queue(channel, self._queue_name)     # for delayed hand-backs
                channel.basic_qos(prefetch_count=1)
                break
            except Exception as e:
                close_quietly(connection)
                if not (self._job_thread is not None and self._job_thread.is_alive()):
                    raise
                logger.error(f"Cannot reconnect to RabbitMQ ({e}), the job keeps running, "
                             f"retrying in {RECONNECT_DELAY_S} s")
                time.sleep(RECONNECT_DELAY_S)
        self._raw_channel = channel
        self._consumer_tag = None
        self._thread_channel = _ConnectionThreadChannel(channel)
        self._run_pending_callbacks()
        thread = self._job_thread
        if thread is not None:
            # a job thread that ended just now may have queued its _after_job
            # on the lost connection
            thread.join(timeout=1)
        if thread is None or not thread.is_alive():
            self._after_job()
        return connection

    def _disconnect(self, connection) -> None:
        self._thread_channel = None
        self._raw_channel = None
        self._consumer_tag = None           # consumers end with their channel
        close_quietly(connection)

    def _run_pending_callbacks(self) -> None:
        while True:
            try:
                fn = self._pending_callbacks.get_nowait()
            except queue.Empty:
                return
            fn()

    def _consume(self) -> None:
        if self._raw_channel is not None:
            self._consumer_tag = self._raw_channel.basic_consume(
                queue=self._queue_name, auto_ack=False, on_message_callback=self._on_message)

    def _after_job(self) -> None:
        """On the connection thread, once a job has ended: take the next one,
        or end the loop when a shutdown was requested."""
        if self.shutdown_handler.check_shutdown():
            self._stop_loop = True
        elif self._consumer_tag is None:
            self._consume()

    def _on_message(self, ch, method, properties, body) -> None:
        """Consumer callback (connection thread). One job at a time per
        worker: the message is acknowledged as soon as the job is claimed, so
        prefetch_count=1 does not hold the next delivery back. The consumer
        is cancelled instead, before the job starts, and re-registered when it
        ends (_after_job). (Otherwise a busy worker would take more jobs, run
        them concurrently and overwrite their lease renewers.)
        In threaded mode the job runs in a thread and the callback returns at
        once, so the connection keeps answering heartbeats."""
        if self._consumer_tag is not None:
            ch.basic_cancel(self._consumer_tag)
            self._consumer_tag = None
        # not idle any more: a SIGTERM now requests a graceful stop of the job
        # instead of exiting the consume loop at once
        self.shutdown_handler.current_job_id = -1
        if not self._threaded:
            try:
                self._handle_message(ch, method, properties, body)
            finally:
                self._after_job()
            return
        self._job_thread = threading.Thread(
            target=self._run_job, args=(_JobChannel(self, ch), method, properties, body),
            name="monolithic-job", daemon=True)
        self._job_thread.start()

    def _job_db(self):
        return db

    def _run_job(self, ch, method, properties, body) -> None:
        """Job-thread body: handle the message, then hand this thread's
        database connection back to the pool. Peewee keeps the connection in
        thread-local state, which vanishes with the thread while the pool
        still counts it as checked out. Without the close every job (even a
        duplicate message) leaks one, until MaxConnectionsExceeded."""
        try:
            self._handle_message(ch, method, properties, body)
        except SystemExit:
            pass        # graceful shutdown: _handle_message already nacked and stopped the loop
        finally:
            try:
                db = self._job_db()
                if not db.is_closed():
                    db.close()
            except Exception as e:      # never let cleanup mask the job's outcome
                logger.warning(f"Could not return the job thread's database connection: {e}")
            # back on the connection thread: next job, or stop
            ch.on_connection_thread(self._after_job)

    def _handle_message(self, ch, method, properties, body) -> None:
        """Handle incoming RabbitMQ message."""

        self._channel = ch

        message = None
        job = None
        # Set once the job is claimed: the message is then acknowledged and the
        # lease (renewed by self._lease_renewer) is the job's only owner
        # record, so a job handed back from here on is republished, not nacked.
        acked = False

        try:
            message = MonolithicJobMessage.from_json(body)
            self.shutdown_handler.current_job_id = message.job_id

            worker_metrics.set_worker_status(
                'monolithic',
                self.compute.name,
                busy=True,
                job_id=message.job_id,
            )

            logger.info("=" * 80)
            logger.info(f"[JOB {message.job_id}] Starting monolithic processing")
            logger.info(f"[JOB {message.job_id}]   epochs: {message.num_of_epochs:,}")
            logger.info(f"[JOB {message.job_id}]   algorithm: {message.force_algorithm}")
            logger.info(f"[JOB {message.job_id}]   integration: {message.integration_method}")
            logger.info("=" * 80)

            job = Job.get(Job.job_id == message.job_id)
            claimed = self._claim(ch, method, properties, job)
            if claimed is None:
                return
            job = claimed

            # Claimed: acknowledge now. Holding the delivery for the whole job
            # would let the broker's consumer_timeout (30 min) cut off and
            # redeliver every longer job. From here the
            # lease is renewed in the background for as long as the job is
            # owned. A dead worker's lease expires and the watchdog
            # republishes the job (watchdog.detect_stale_jobs).
            ch.basic_ack(delivery_tag=method.delivery_tag)
            acked = True
            self._lease_renewer = LeaseRenewer(job.job_id, self.worker_id).start()

            # A resumed run (the previous lease holder died, was evicted or
            # lost its lease) bumps the never-reset recovery_count, so
            # experiments can tell interrupted runs apart afterwards.
            record_recovery_if_resumed(job, method.redelivered)

            if not self._finalized_at_final_epoch(job, message):
                self._integrate(job, message, method.redelivered)

        except LeaseLostException:
            job_id = message.job_id if message else '?'
            # another worker holds the job now (a takeover after our lease
            # lapsed): it is that worker's to finish, nothing to hand back
            logger.warning(f"[JOB {job_id}] Lease lost to another worker, leaving the job to it")
            self._stop_lease_renewer()
            if not acked:
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

        except ShutdownException as e:
            job_id = message.job_id if message else '?'
            logger.warning(f"[JOB {job_id}] Graceful shutdown: {e}")
            if acked and job:
                self._hand_back(ch, job)       # another worker resumes from the last checkpoint
            else:
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                if job:
                    release_job_lease(job.job_id, self.worker_id)
            sys.exit(0)

        except JobCancelledException:
            job_id = message.job_id if message else '?'
            logger.info(f"[JOB {job_id}] Job cancelled by user")
            if job:
                # only the status: `job` was loaded at the claim and progress
                # has since been written with Job.update, so job.save() would
                # put the claim-time epoch, retry and recovery counts back
                Job.update(status=JobStatus.CANCELLED.value, updated_at=utcnow()).where(
                    Job.job_id == job.job_id).execute()
            if not acked:
                ch.basic_ack(delivery_tag=method.delivery_tag)
            self._stop_lease_renewer()
            if job:
                release_job_lease(job.job_id, self.worker_id)

        except JobPausedException:
            job_id = message.job_id if message else '?'
            logger.info(f"[JOB {job_id}] Job paused by user, progress saved (resume will republish)")
            if not acked:
                ch.basic_ack(delivery_tag=method.delivery_tag)
            self._stop_lease_renewer()
            if job:
                release_job_lease(job.job_id, self.worker_id)

        except ComputeError as e:
            self._on_compute_error(ch, method, properties, body, message, job, acked, e)

        except Job.DoesNotExist:
            job_id = message.job_id if message else '?'
            logger.warning(f"[JOB {job_id}] Job not found in database, discarding message")
            if not acked:
                ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            self._on_failure(ch, method, properties, body, message, job, acked, e)

        finally:
            self._stop_lease_renewer()
            # a job that ended before drain() (pause, cancel, lost lease,
            # shutdown, failure) leaves its uploads to land, and their error
            # with it instead of failing the next job's first snapshot
            try:
                settle_snapshot_uploads()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Could not settle the snapshot uploads: {e}")
            self._channel = None
            self.shutdown_handler.current_job_id = None
            if self.shutdown_handler.check_shutdown():
                # a signal during the job (after its last checkpoint) was already
                # consumed. Don't go back to waiting for work
                ch.stop_consuming()
            worker_metrics.set_worker_status('monolithic', self.compute.name, busy=False)

    def _claim(self, ch, method, properties, job):
        """Take the job's lease. Returns the job as it stands once claimed, or
        None when the message was settled here instead: a duplicate of a job a
        live worker holds, a shutdown during the wait, or a job with nothing
        left to run (completed, failed, cancelled, paused).

        A fresh message whose job is leased is a duplicate (a watchdog
        republish) of one a live worker is processing: drop it via ACK. A
        REDELIVERED one comes from a consumer whose connection died, usually a
        killed worker whose lease has not expired yet. It waits for the lease
        to be renewed (holder alive: drop) or to expire (holder dead: take
        over), instead of leaving the job to the watchdog's 30-minute rule.

        We intentionally do NOT `basic_nack(requeue=True)`: with
        `prefetch_count=1` that creates a tight nack-redeliver loop against the
        same poison message (tens of nacks per second per stuck worker),
        starving the worker from picking up real work in the queue."""
        if not acquire_job_lease(job.job_id, self.worker_id, handback_marker(properties)):
            outcome = 'alive'
            if method.redelivered:
                outcome = wait_out_dead_lease(
                    job.job_id, self.worker_id, sleep=ch.connection.sleep,
                    should_stop=self.shutdown_handler.check_shutdown)
            if outcome == 'stopped':
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                return None
            if outcome != 'acquired':
                logger.warning(
                    f"[JOB {job.job_id}] Already leased by another worker — "
                    f"discarding duplicate message"
                )
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return None
            job = Job.get(Job.job_id == job.job_id)   # state after the wait

        logger.info(f"[JOB {job.job_id}] Lease acquired by {self.worker_id}")

        if job.status == JobStatus.COMPLETED.value:
            reason = "Already completed, skipping"
        elif job.status in (JobStatus.FAILED.value, JobStatus.CANCELLED.value):
            reason = f"Job in terminal state: {job.status}"
        elif job.status == JobStatus.PAUSED.value:
            reason = "Job is paused, discarding message (resume will republish)"
        else:
            return job
        logger.info(f"[JOB {job.job_id}] {reason}")
        release_job_lease(job.job_id, self.worker_id)
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return None

    def _finalized_at_final_epoch(self, job, message) -> bool:
        """A job already at its final epoch (a worker died after the last
        epoch): finalized here if its final snapshot landed (True). If not,
        the pointer ran ahead of the final upload, and it is moved back so the
        run resumes from the last checkpoint before the final epoch."""
        if job.current_epoch < message.num_of_epochs:
            return False
        if final_snapshot_is_durable(job.job_id, message.num_of_epochs):
            logger.info(f"[JOB {job.job_id}] At final epoch, finalizing only")
            finalize_job(job, message)
            self._stop_lease_renewer()
            release_job_lease(job.job_id, self.worker_id)
            return True
        logger.warning(f"[JOB {job.job_id}] At final epoch but its snapshot is missing, "
                       f"recomputing from the last checkpoint")
        job.current_epoch = message.num_of_epochs - 1
        Job.update(current_epoch=job.current_epoch, updated_at=utcnow()).where(
            Job.job_id == job.job_id).execute()
        return False

    def _integrate(self, job, message, redelivered: bool) -> None:
        """Load the bodies (the latest checkpoint for a resumed job), run the
        remaining epochs and finalize the job."""
        all_bodies, start_epoch = load_bodies_for_job(job, message, redelivered, logger)

        logger.info(f"[JOB {message.job_id}] Loaded {len(all_bodies)} bodies")
        # Start the clock after the input is loaded, as the distributed
        # pipeline does (its first task starts after the API has parsed
        # the file). completed_at is taken when the epoch loop and the
        # final snapshot upload finish, before finalize_job's snapshot
        # combination, so completed_at - started_at spans the integration
        # (plus, for a recovered run, the stall and the recomputation).
        # The mark is set once: a resumed job keeps its first start.
        mark_job_started(job.job_id)
        record_compute_hosts(job.job_id, [compute_host()])

        # Process all epochs (returns after the final snapshot upload)
        self._process_all_epochs(all_bodies, job, MonolithicJobConfig.from_message(message), start_epoch)
        integration_completed_at = utcnow()

        # Finalize (combines snapshots, not part of the measured duration)
        finalize_job(job, message, integration_completed_at=integration_completed_at)
        self._stop_lease_renewer()
        release_job_lease(job.job_id, self.worker_id)

        logger.info(f"[JOB {message.job_id}] Completed successfully")

    def _on_compute_error(self, ch, method, properties, body, message, job, acked, e) -> None:
        """A job whose backend raised ComputeError `e`: failed if the error is
        permanent, else handed back or requeued. An unusable device also
        restarts the worker."""
        job_id = message.job_id if message else '?'
        logger.error(f"[JOB {job_id}] Compute error: {e}")
        if e.restart_worker:
            # the device is unusable here: the job goes back, counted
            # against its retry budget (a job that faults every time
            # fails), and the worker restarts
            self._on_failure(ch, method, properties, body, message, job, acked, e)
            exit_for_restart(str(e))
        if not e.recoverable:
            if message:
                Job.update(
                    status=JobStatus.FAILED.value,
                    updated_at=utcnow(),
                ).where((Job.job_id == message.job_id) & Job.status.not_in(_FINAL)).execute()
            if not acked:
                ch.basic_ack(delivery_tag=method.delivery_tag)
        elif acked and job:
            self._hand_back(ch, job)
            return
        else:
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        self._stop_lease_renewer()
        if message:
            release_job_lease(message.job_id, self.worker_id)

    def _on_failure(self, ch, method, properties, body, message, job, acked, e) -> None:
        """A job that failed with `e`: hand it back or requeue it, counted
        against its retry budget unless an infrastructure outage caused it."""
        job_id = message.job_id if message else '?'
        logger.error(f"[JOB {job_id}] Failed: {e}", exc_info=True)
        # Poison-message guard: an unhandled error that is PERMANENT
        # (e.g. corrupt input) would otherwise hot-loop — nack(requeue) →
        # immediate redelivery → fail → nack … — and because the lease is
        # released each cycle the watchdog may never see it as stale. Cap
        # requeues with the job's retry_count (reset on progress). Once the
        # budget is spent, mark the job FAILED and drop the message.
        # An infrastructure outage (S3, PostgreSQL) is not the job's fault:
        # it is retried after a delay, uncounted, however long it lasts.
        requeue = True
        transient = message is not None and is_transient_error(e)
        if message is None:
            # the message itself is malformed: no redelivery can parse it
            requeue = False
            logger.error("Dropping a message that could not be parsed")
        elif transient:
            logger.warning(f"[JOB {job_id}] Transient infrastructure error, retrying in "
                           f"{TRANSIENT_HANDBACK_DELAY_MS / 1000:.0f} s, retry budget untouched")
        else:
            try:
                Job.update(
                    retry_count=Job.retry_count + 1,
                    updated_at=utcnow(),
                ).where(Job.job_id == message.job_id).execute()
                attempts = Job.select(Job.retry_count).where(
                    Job.job_id == message.job_id).scalar() or 0
                if attempts > MAX_REPUBLISH_ATTEMPTS:
                    Job.update(
                        status=JobStatus.FAILED.value,
                        updated_at=utcnow(),
                    ).where((Job.job_id == message.job_id) & Job.status.not_in(_FINAL)).execute()
                    requeue = False
                    logger.error(
                        f"[JOB {job_id}] Exceeded {MAX_REPUBLISH_ATTEMPTS} "
                        f"retries without progress, marking FAILED and dropping message"
                    )
            except Exception as track_err:
                logger.warning(f"[JOB {job_id}] Could not update retry budget: {track_err}")
        if acked and job and requeue:
            self._hand_back(ch, job, delayed=transient)
            return
        if not acked:
            self._settle_unclaimed(ch, method, properties, body, requeue, transient)
        self._stop_lease_renewer()
        if message:
            release_job_lease(message.job_id, self.worker_id)

    def _service_heartbeats(self) -> None:
        """Synchronous mode (GPU): the job runs inside the consumer callback,
        so the connection is serviced here, between epochs (at most once a
        second), or a job longer than the heartbeat timeout loses it. The
        nested process_data_events dispatches no deliveries (pika guards the
        reentrancy, the consumer is cancelled anyway). A lost connection does
        not stop the job, whose message was acknowledged: the loop reconnects
        once it ends."""
        ch = getattr(self, '_channel', None)
        if getattr(self, '_threaded', True) or ch is None:
            return
        now = time.monotonic()
        if now - getattr(self, '_last_heartbeat_service', 0.0) < 1.0:
            return
        self._last_heartbeat_service = now
        try:
            ch.connection.process_data_events(time_limit=0)
        except RABBITMQ_CHANNEL_ERRORS as e:
            logger.warning(f"Lost the RabbitMQ connection during the job ({e!r}), "
                           f"the job continues, the worker reconnects after it")
            self._channel = None

    def _stop_lease_renewer(self) -> None:
        renewer = getattr(self, '_lease_renewer', None)
        if renewer is not None:
            renewer.stop()
            self._lease_renewer = None

    def _settle_unclaimed(self, ch, method, properties, body, requeue: bool, transient: bool) -> None:
        """Settle the delivery of a job that failed before its claim. An
        outage's message waits in the retry queue: a nack-requeue would hand
        it straight back, a hot loop for as long as the outage lasts."""
        queue = getattr(self, '_queue_name', None)
        if transient and requeue and queue:
            try:
                retry_later(ch, method.delivery_tag, queue, body, properties, count=False)
                return
            except Exception as e:  # noqa: BLE001 — requeued at once instead
                logger.warning(f"Could not delay the retry ({e!r}), requeueing")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=requeue)

    def _hand_back(self, ch, job, delayed: bool = False) -> None:
        """Give a claimed job back for another worker to resume from its last
        checkpoint (graceful shutdown, retryable failure). The message was
        acknowledged at the claim, so it cannot be requeued: expire our lease,
        keeping it ours, then publish the job again. If the publish fails the
        watchdog finds the expired lease and republishes. The order never
        leaves a running job with neither an owner nor a message.
        delayed (an infrastructure outage): the job reaches the queue only
        after TRANSIENT_HANDBACK_DELAY_MS, through its retry queue.

        The database may be the outage: then the lease cannot be expired nor
        the job re-read. The message is published anyway, built from the job
        as loaded, and marked as handed back by this worker (handback_headers),
        so its consumer takes over our unexpired lease (acquire_job_lease)
        once the database is back, instead of the job idling until the lease
        expires and the watchdog republishes it (about 11 min)."""
        self._stop_lease_renewer()
        expired = expire_job_lease(job.job_id, self.worker_id, delayed=delayed)
        # after the renewer stopped: no renewal of ours is later than this
        headers = handback_headers(self.worker_id, utcnow())
        try:
            try:
                fresh = Job.get(Job.job_id == job.job_id)
            except Exception as e:  # noqa: BLE001
                if not is_transient_error(e):
                    raise
                logger.warning(f"[JOB {job.job_id}] Database unavailable ({e}), "
                               f"republishing the job as loaded")
                fresh = job
            queue = getattr(self, '_queue_name', None)
            if not queue:
                queue = GPU_MONOLITHIC_QUEUE if fresh.gpu_enabled else MONOLITHIC_QUEUE
            body = MonolithicJobMessage.from_job(fresh).to_json()
            if delayed:
                publish_later(ch, queue, body, delay_ms=TRANSIENT_HANDBACK_DELAY_MS, headers=headers)
            else:
                ch.basic_publish(exchange='', routing_key=queue, body=body,
                                 properties=pika.BasicProperties(delivery_mode=2, headers=headers))
            logger.warning(f"[JOB {job.job_id}] Handed back: "
                           f"{'lease expired' if expired else 'lease left to the handed-back message'}"
                           f", job republished to {queue}"
                           + (f" in {TRANSIENT_HANDBACK_DELAY_MS / 1000:.0f} s" if delayed else ""))
        except Exception as e:  # noqa: BLE001 — the watchdog republishes an expired lease
            logger.error(f"[JOB {job.job_id}] Could not republish ({e}), the watchdog will, "
                         f"once it sees the expired lease")

    def _process_all_epochs(
        self,
        all_bodies: List[Dict],
        job,
        config: MonolithicJobConfig,
        start_epoch: int = 1,
    ) -> None:
        """Run the job's epochs from start_epoch (above 1 for a resumed job)
        to the last. Returns once the final snapshot has landed."""

        start_time = time.time()
        last_lease_renewal = time.time()

        use_velocity_verlet = is_velocity_verlet(config.integration_method)
        # Snapshots carry the accelerations of the step that produced them for
        # velocity Verlet (its next step needs them) and for adaptive jobs (the
        # next step size is chosen from them), so a restart continues as
        # the uninterrupted run would.
        keep_accelerations = use_velocity_verlet or bool(config.adaptive_enabled)
        s = self._initial_state(all_bodies, job, config, start_epoch, keep_accelerations)
        use_resident = self._load_resident_state(s, job, config)

        logger.info(f"[JOB {job.job_id}] Starting epoch loop: {start_epoch:,} to {config.num_epochs:,}")
        if config.adaptive_enabled:
            logger.info(f"[JOB {job.job_id}] Adaptive dt: eta={config.adaptive_eta}, "
                        f"dt_min={config.adaptive_dt_min:.2e}, dt_max={config.adaptive_dt_max:.2e}")

        computation_start = time.time()
        batch_start = time.time()
        batch_epoch_count = 0

        # The retry budget is reset only by progress past the furthest
        # epoch an earlier delivery reached (recomputing from a checkpoint is
        # not progress), and the final epoch resets it only once its snapshot
        # has landed (below, after drain), so a persistent upload failure
        # exhausts the budget instead of recomputing the job forever.
        progress_mark = int(job.current_epoch or 0)
        softening = config.softening_sq ** 0.5

        # Energy at the run's first snapshot, so |dE/E0| is logged per
        # snapshot thereafter.
        energy_baseline = None

        # progress is written every PROGRESS_LOG_INTERVAL epochs and at least
        # every PROGRESS_MAX_INTERVAL_S: the watchdog takes a leased job whose
        # updated_at is 30 min old for hung, which slow epochs must not mimic
        last_progress_write = time.time()

        for epoch in range(start_epoch, config.num_epochs + 1):
            last_lease_renewal = self._check_lease(job, last_lease_renewal)

            if self.shutdown_handler.check_shutdown():
                raise ShutdownException(f"Shutdown at epoch {epoch}")

            self._service_heartbeats()

            # Adaptive dt estimation (from previous step's accelerations)
            if config.adaptive_enabled and s.accelerations is not None:
                s.dt = estimate_adaptive_dt(
                    s.accelerations, s.dt,
                    eta=config.adaptive_eta,
                    softening=softening,
                    dt_min=config.adaptive_dt_min,
                    dt_max=config.adaptive_dt_max,
                )

            self._step(s, config, use_resident, use_velocity_verlet)
            s.simulation_time += s.dt

            if should_save_snapshot(epoch, config.snapshot_interval, config.num_epochs):
                if use_resident:
                    s.positions, s.velocities, s.accelerations = self.compute.fetch_resident_state()
                energy_baseline = self._snapshot(job, config, epoch, s, keep_accelerations, energy_baseline)

            batch_epoch_count += 1

            if (epoch % PROGRESS_LOG_INTERVAL == 0 or epoch == config.num_epochs
                    or time.time() - last_progress_write > PROGRESS_MAX_INTERVAL_S):
                last_progress_write = time.time()
                self._write_progress(job, config, epoch, start_epoch, s, progress_mark, computation_start)

                worker_metrics.observe_epoch_batch(
                    compute_type=self.compute.name,
                    algorithm=config.algorithm,
                    batch_time=time.time() - batch_start,
                    epoch_count=batch_epoch_count,
                )
                batch_start = time.time()
                batch_epoch_count = 0

                self._check_cancel_pause_shutdown(job, epoch)

        # All queued snapshot uploads must land before the job is reported
        # complete (resume and combine read them). Raises on upload failure.
        get_snapshot_uploader().drain()
        Job.update(retry_count=0, updated_at=utcnow()).where(
            (Job.job_id == job.job_id) & (Job.worker_id == self.worker_id)).execute()

        total_time = time.time() - start_time
        computation_time = time.time() - computation_start

        worker_metrics.observe_monolithic_job(
            compute_type=self.compute.name,
            algorithm=config.algorithm,
            total_time=total_time,
        )

        logger.info(f"[JOB {job.job_id}] Completed {config.num_epochs:,} epochs")
        logger.info(
            f"[JOB {job.job_id}] Total: {total_time/3600:.2f}h, "
            f"Rate: {config.num_epochs/computation_time:.2f} epoch/s"
        )

    def _initial_state(self, all_bodies: List[Dict], job, config: MonolithicJobConfig, start_epoch: int,
                       keep_accelerations: bool) -> _RunState:
        """The loaded bodies as arrays, with the accelerations their snapshot
        carries (kept ones only, None makes the first step compute them), and
        the step size to continue with."""
        if keep_accelerations:
            positions, velocities, masses, ids, accelerations, has_accel = \
                bodies_to_numpy_with_accelerations(all_bodies)
            if not has_accel:
                accelerations = None  # Will be computed on first step
        else:
            positions, velocities, masses, ids = bodies_to_numpy(all_bodies)
            accelerations = None

        # A restart of an adaptive job continues from the step the
        # checkpoint's epoch took (its metadata sidecar, read by
        # load_bodies_for_job), not from the initial step.
        dt = config.time_step
        if config.adaptive_enabled and start_epoch > 1:
            resume_dt = getattr(job, 'resume_dt', None)
            if resume_dt:
                dt = float(resume_dt)
            else:
                logger.warning(f"[JOB {job.job_id}] adaptive restart without the checkpoint's step size, "
                               f"continuing from the initial step {config.time_step:g}")
        return _RunState(
            positions, velocities, masses, ids, accelerations,
            ns=np.array([int(b.get('n', i)) for i, b in enumerate(all_bodies)], dtype=np.int64),
            dt=dt, simulation_time=job.current_simulation_time or 0.0)

    def _load_resident_state(self, s: _RunState, job, config: MonolithicJobConfig) -> bool:
        """Device-resident fast path: the backend (GPU) keeps body state in
        device memory between epochs, so the per-epoch host<->device
        round-trip disappears. State is fetched back only on snapshot epochs
        and at the end. For adaptive dt, only the (small) acceleration buffer
        is read back each epoch. True if the state was loaded."""
        if not (self.compute.supports_resident_state and config.algorithm == 'direct'):
            return False
        self.compute.load_resident_state(
            s.positions, s.velocities, s.masses, s.ids,
            time_step=config.time_step,
            integration_method=config.integration_method,
            softening_sq=config.softening_sq,
        )
        logger.info(f"[JOB {job.job_id}] Using device-resident stepping on {self.compute.name}")
        return True

    def _check_lease(self, job, last_lease_renewal: float) -> float:
        """LeaseLostException once the job's lease is gone. The lease is renewed
        in the background (LeaseRenewer) when the job was claimed through
        _handle_message, here only when the loop runs without one. Returns the
        time of the last renewal."""
        renewer = getattr(self, '_lease_renewer', None)
        if renewer is not None:
            if renewer.lost:
                raise LeaseLostException("Lease lost to another worker")
        elif time.time() - last_lease_renewal > LEASE_RENEWAL_INTERVAL_SECONDS:
            if not renew_job_lease(job.job_id, self.worker_id):
                raise LeaseLostException("Lease lost to another worker")
            return time.time()
        return last_lease_renewal

    def _step(self, s: _RunState, config: MonolithicJobConfig, use_resident: bool,
              use_velocity_verlet: bool) -> None:
        """One step of s.dt. Resident: the state advances in device memory, the
        host copy is refreshed only when needed (snapshots, end)."""
        if use_resident:
            stepped_accel = self.compute.step_resident(s.dt, need_accelerations=config.adaptive_enabled)
            if stepped_accel is not None:
                s.accelerations = stepped_accel
            return
        s.positions, s.velocities, s.accelerations = self.compute.compute_step(
            positions=s.positions,
            velocities=s.velocities,
            masses=s.masses,
            ids=s.ids,
            time_step=s.dt,
            algorithm=config.algorithm,
            integration_method=config.integration_method,
            barnes_hut_theta=config.barnes_hut_theta,
            softening_sq=config.softening_sq,
            previous_accelerations=s.accelerations if use_velocity_verlet else None,
        )

    def _snapshot(self, job, config: MonolithicJobConfig, epoch: int, s: _RunState,
                  keep_accelerations: bool, energy_baseline: Optional[float]) -> Optional[float]:
        """Save the epoch's snapshot, serialized straight from numpy and
        uploaded on a background thread, so S3 latency does not stall the
        epoch loop (drained before completion). Returns the energy baseline."""
        # Abort on a blown-up (non-finite) state rather than persist
        # NaN/Inf snapshots that look "completed" downstream.
        # Not retryable: the computation is deterministic, so a
        # retry diverges the same way (the handler marks it FAILED).
        if not is_finite_state(s.positions, s.velocities):
            raise ComputeError(
                self.compute.name,
                f"non-finite (NaN/Inf) state at epoch {epoch}, aborting to avoid "
                f"persisting a diverged trajectory (check softening_sq/time_step)",
                recoverable=False,
            )

        # Per-snapshot energy and centre-of-mass drift, an O(N^2) pass:
        # only with the snapshot diagnostics on, as on the distributed
        # path, so timed runs of both engines do the same work
        if task_timing.DIAGNOSTICS_ENABLED:
            ke, pe = total_energy(s.positions, s.velocities, s.masses, config.softening_sq, G_CONST)
            energy = ke + pe
            if energy_baseline is None:
                energy_baseline = energy
            e_drift = (abs((energy - energy_baseline) / energy_baseline)
                       if energy_baseline else float('nan'))
            com_pos_off, com_vel_off = com_drift(s.positions, s.velocities, s.masses)
            logger.info(
                f"[JOB {job.job_id}] epoch {epoch}: E={energy:.6e} "
                f"|dE/E0|={e_drift:.2e} COMdrift(x={com_pos_off:.2e}, v={com_vel_off:.2e})"
            )

        upload_snapshot_from_arrays(
            job.job_id, epoch, s.ids, s.masses, s.positions, s.velocities,
            accelerations=s.accelerations if keep_accelerations else None,
            ns=s.ns,
        )
        if config.adaptive_enabled:
            upload_snapshot_metadata(job.job_id, epoch, s.simulation_time, s.dt)
        return energy_baseline

    def _write_progress(self, job, config: MonolithicJobConfig, epoch: int, start_epoch: int,
                        s: _RunState, progress_mark: int, computation_start: float) -> None:
        """Log the progress and write it to the job row (LeaseLostException if
        another worker holds the job now)."""
        elapsed = time.time() - computation_start
        epochs_done = epoch - start_epoch + 1

        if epochs_done > 0:
            avg_time = elapsed / epochs_done
            eta_seconds = avg_time * (config.num_epochs - epoch)
            eta_str = f'{eta_seconds/60:.1f} min' if eta_seconds > 60 else f'{eta_seconds:.1f} sec'
            percent = 100.0 * epoch / config.num_epochs

            dt_info = f" | dt={s.dt:.2e}, sim_t={s.simulation_time:.4e}" if config.adaptive_enabled else ""
            logger.info(
                f"[JOB {job.job_id}] Epoch {epoch:,}/{config.num_epochs:,} "
                f"({percent:.1f}%) | ETA: {eta_str}{dt_info}"
            )

        # Progress resets the retry budget: a worker that advances
        # epochs is forgiven earlier watchdog republishes, while a job
        # that makes no forward progress (see progress_mark) still
        # reaches the retry ceiling. (Whether the run was ever
        # interrupted is kept in recovery_count, which is never reset.)
        progress = dict(current_epoch=epoch, current_simulation_time=s.simulation_time,
                        updated_at=utcnow())
        if progress_mark < epoch < config.num_epochs:
            progress['retry_count'] = 0
        # Only while this worker holds the job: after a takeover the
        # other worker's progress must not be overwritten
        if not Job.update(**progress).where(
                (Job.job_id == job.job_id) & (Job.worker_id == self.worker_id)).execute():
            raise LeaseLostException("Lease lost to another worker")

    def _check_cancel_pause_shutdown(self, job, epoch: int) -> None:
        """Stop at a saved epoch for a cancel or pause (the job row) or a
        shutdown, by raising the matching exception."""
        job_fresh = Job.get(Job.job_id == job.job_id)
        if job_fresh.status == JobStatus.CANCELLED.value:
            raise JobCancelledException(f"Job cancelled at epoch {epoch}")
        if job_fresh.status == JobStatus.PAUSED.value:
            raise JobPausedException(f"Job paused at epoch {epoch}")
        if self.shutdown_handler.check_shutdown():
            raise ShutdownException(f"Shutdown at epoch {epoch} (saved)")
