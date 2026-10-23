# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Distributed worker - processes packages using pluggable backends.

DistributedWorker is the claim/compute/acknowledge loop both pipelines share,
DistributedWorkerV1 the v1 worker (RabbitMQ, entry points worker.py and
worker_gpu.py). The v2 worker (Redis Streams) is in distributed_worker_v2.py.
It uses:
- ComputeBackend (CPU or GPU) for physics computation
- MessageBackend (RabbitMQ or Redis) for task queue management
"""

import io
import os
import signal
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np
import pika

import worker_metrics
from file_service import PARTIAL_RESULTS_BUCKET_NAME, ensure_buckets_exist, upload_file
from job_timestamps import mark_job_started
from logging_config import setup_logging
from models import Job, Package
from rabbitmq import TO_MERGE_QUEUE
from schemas import JobStatus, MergerMessage, PackageStatus
from task_timing import PhaseTimer
from transient_errors import is_transient_error
from worker_utils import bodies_to_numpy, get_n_bodies, get_partial_results_file
from workers.backends.rabbitmq_backend import RabbitMQBackend
from workers.base.compute_backend import ComputeBackend, ComputeError, exit_for_restart
from workers.base.message_backend import MessageBackend, MessageBackendError, TaskMessage


logger = setup_logging('distributed-worker')

# Longest wait in one claim call. Also how long an idle worker takes to notice SIGTERM.
CLAIM_BLOCK_MS = 1000

# How often a worker looks for stale tasks to reclaim (seconds).
RECLAIM_INTERVAL_S = 2.0

# Backoff between attempts to reconnect a lost message backend (seconds).
RECONNECT_DELAY_MIN_S = 1.0
RECONNECT_DELAY_MAX_S = 30.0


# A task delivered more often than this fails its job instead of being
# redelivered forever (the delivery over the cap is still attempted once, see
# _run_loop). v2 counts every delivery (a worker's death too). v1's classic
# queues only count the failures it requeues itself (rabbitmq.retry_later).
MAX_TASK_DELIVERIES = int(os.environ.get('NBODY_MAX_TASK_DELIVERIES', '5'))


class DistributedWorker:
    """Unified distributed worker using pluggable compute and message backends."""

    def __init__(
        self,
        compute_backend: ComputeBackend,
        message_backend: MessageBackend,
        worker_name: Optional[str] = None,
    ):
        """A worker computing with compute_backend (CPU or GPU) on the tasks of
        message_backend (RabbitMQ or Redis). worker_name is generated if not given."""
        self.compute = compute_backend
        self.messages = message_backend
        # Pod name plus a short suffix: unique per process (several workers may
        # share a host outside Kubernetes) and traceable to the pod that holds
        # a merge lease or a pending task.
        host = os.environ.get('HOSTNAME')
        self.worker_name = worker_name or (
            f"{host}-{uuid.uuid4().hex[:4]}" if host else f"worker-{uuid.uuid4().hex[:8]}")

        # Shutdown handling
        # Plain bools, written without a lock: the SIGTERM handler runs on the
        # main thread between bytecodes, so taking a lock the loop may hold
        # would deadlock, and single attribute writes are atomic.
        self._shutdown_requested = False
        self._processing_task = False
        self._last_reclaim = float('-inf')
        self._reconnect_delay = RECONNECT_DELAY_MIN_S
        self._timer = None                  # the current task's PhaseTimer (_process_task)
        self._task_bodies = 0               # the current task's package size, once known
        self._task_completed = False        # its result was handled
        self._last_failure_transient = False  # the last task failed on an S3 or database outage
        self._restart_reason = None         # set by a compute error that broke the device

        # Queue name (set during run)
        self._queue_name: Optional[str] = None

        # Callback for post-processing (e.g., notify merger, store results)
        self._result_handler: Optional[Callable] = None

        logger.info(f"DistributedWorker initialized: {self.worker_name}")
        logger.info(f"  Compute backend: {self.compute.name}")
        logger.info(f"  Message backend: {self.messages.name}")

    def run(
        self,
        queue_name: str,
        result_handler: Optional[Callable] = None,
        warmup: bool = True,
    ) -> None:
        """The worker's main loop on queue_name. result_handler: an optional
        callback for the results. warmup: JIT-compile the compute backend first."""
        self._queue_name = queue_name
        self._result_handler = result_handler

        # Setup signal handlers
        self._setup_signal_handlers()

        # Connect to message backend
        logger.info(f"Connecting to {self.messages.name}...")
        self.messages.connect()
        self.messages.ensure_queues()
        logger.info(f"Connected to {self.messages.name}")

        # Warmup compute backend
        if warmup:
            logger.info(f"Warming up {self.compute.name} backend...")
            try:
                self.compute.warmup()
                logger.info(f"{self.compute.name} backend warmed up")
            except ComputeError as e:
                logger.error(f"Compute backend warmup failed: {e}")
                if not e.recoverable:
                    raise

        logger.info(f"Worker {self.worker_name} ready, consuming from '{queue_name}'...")

        # Main loop
        try:
            self._run_loop()
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            self._cleanup()

    def _run_loop(self) -> None:
        """Main worker loop."""
        while not self._shutdown_requested:
            try:
                # Finish any epoch whose merging worker died (rate-limited)
                self._on_idle()

                # First try to claim stale tasks (crashed workers, or tasks
                # given up on). Tasks only become stale after a timeout of
                # seconds to minutes, so scan for them every RECLAIM_INTERVAL_S
                # rather than with an extra round trip before every task.
                task = None
                now = time.monotonic()
                if now - self._last_reclaim >= RECLAIM_INTERVAL_S:
                    self._last_reclaim = now
                    task = self.messages.claim_stale_task(
                        consumer_name=self.worker_name,
                        queue_name=self._queue_name,
                    )

                if task:
                    logger.info(f"Reclaimed stale task: {task.message_id}")
                else:
                    # Get new task. The short block bounds how long a SIGTERM
                    # waits for the loop to notice it (see _handle_signal).
                    task = self.messages.claim_task(
                        consumer_name=self.worker_name,
                        queue_name=self._queue_name,
                        block_ms=CLAIM_BLOCK_MS,
                    )

                if not task:
                    continue

                # A task claimed after SIGTERM is still processed: it is in this
                # consumer's pending list, and dropping it would leave it there
                # until another worker reclaims it after STALE_TASK_MS.

                # Mark as processing for graceful shutdown
                self._processing_task = True
                # Keep the task from being reclaimed as stale while we work on
                # it: only a dead worker's tasks should count as redeliveries.
                heartbeat = self.messages.keep_alive(task, self.worker_name)

                try:
                    deliveries = int((task.raw_data or {}).get('__deliveries__', 0) or 0)
                    if deliveries > MAX_TASK_DELIVERIES and self._should_skip(task):
                        # a stale task (old generation, cancelled job) must not
                        # fail the job just because it was redelivered often
                        self.messages.ack_task(task)
                        continue
                    if deliveries > MAX_TASK_DELIVERIES + 1:
                        # Past the one attempt over the cap (below): only Redis
                        # gets here, as it counts every delivery, a dead
                        # worker's too, so a task that kills its worker is not
                        # attempted forever.
                        self._fail_or_release(task, heartbeat, f"Task (epoch {task.epoch_num}, package "
                                                               f"{task.package_num}) delivered {deliveries} times")
                        continue

                    success = self._process_task(task)

                    if success:
                        self.messages.ack_task(task)
                    elif self._job_is_gone(task.job_id):
                        logger.warning(f"Job {task.job_id} not found, discarding message")
                        self.messages.ack_task(task)
                    elif deliveries > MAX_TASK_DELIVERIES and not self._last_failure_transient:
                        # The delivery over the cap is still attempted, and
                        # only its failure fails the job (an outage that ended
                        # meanwhile does not): one attempt more than
                        # v1_epochs.retry_or_fail allows a merge or epoch step.
                        self._fail_or_release(task, heartbeat, f"Task (epoch {task.epoch_num}, package "
                                                               f"{task.package_num}) failed {deliveries} times")
                    else:
                        # an S3 or PostgreSQL outage is retried without counting
                        # towards the cap: it would fail a healthy job (v1,
                        # Redis counts every delivery regardless)
                        self._retry_later(task, heartbeat, count=not self._last_failure_transient)

                finally:
                    if heartbeat is not None:
                        heartbeat.stop()
                    self._processing_task = False

                if self._restart_reason:
                    # the task is handed back above
                    exit_for_restart(self._restart_reason)

                # Check shutdown after task completion
                if self._shutdown_requested:
                    logger.info("Shutdown requested, exiting after task completion")
                    break

            except MessageBackendError as e:
                logger.error(f"Message backend error: {e}")
                if e.recoverable:
                    self._recover_backend()
                else:
                    raise
            except Exception as e:
                logger.error(f"Error in worker loop: {e}", exc_info=True)
                time.sleep(1)

    def _retry_later(self, task: TaskMessage, heartbeat, count: bool = True) -> None:
        """Nack a task for redelivery after the backend's retry delay. The
        heartbeat stops first: a tick after the nack would reset the idle
        time the delay is set by."""
        if heartbeat is not None:
            heartbeat.stop()
        self.messages.nack_task(task, requeue=True, count=count)

    def _fail_or_release(self, task: TaskMessage, heartbeat, reason: str) -> None:
        """Fail the job of a task over the delivery cap and drop the task. If
        the job could not be failed (database or Redis unreachable), the task
        is kept for another try: left pending where the backend reclaims stale
        tasks (Redis), otherwise nacked without counting, as an unacknowledged
        RabbitMQ delivery would block this consumer (prefetch) until the
        broker's consumer timeout."""
        if self._fail_job(task, reason):
            self.messages.ack_task(task)
        elif not self.messages.reclaims_stale_tasks:
            self._retry_later(task, heartbeat, count=False)

    def _recover_backend(self) -> None:
        """After a recoverable backend error: reconnect if the connection is
        gone (connect() runs once, in run(), without this a v1 worker whose
        RabbitMQ connection dropped failed every claim forever), backing off
        between attempts. Otherwise just pause. A task claimed on the lost
        connection is not acknowledged on the new one: the broker redelivers it."""
        try:
            connected = self.messages.is_connected()
        except Exception:  # noqa: BLE001
            connected = False
        if connected:
            time.sleep(1)
            return
        delay = self._reconnect_delay
        logger.warning(f"{self.messages.name} connection lost, reconnecting in {delay:.0f} s")
        deadline = time.monotonic() + delay
        while not self._shutdown_requested and time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        if self._shutdown_requested:
            return
        try:
            self.messages.disconnect()
            self.messages.connect()
            self.messages.ensure_queues()
        except Exception as e:  # noqa: BLE001 — retried with a longer delay
            self._reconnect_delay = min(delay * 2, RECONNECT_DELAY_MAX_S)
            logger.error(f"Reconnecting to {self.messages.name} failed: {e}")
            return
        self._reconnect_delay = RECONNECT_DELAY_MIN_S
        logger.info(f"Reconnected to {self.messages.name}")

    # worker_type label of the Prometheus metrics (as the entry point's
    # worker_metrics.start_metrics_server call names it)
    metrics_worker_type = 'distributed'

    def _process_task(self, task: TaskMessage) -> bool:
        """Process a task, reporting it to the worker metrics. True if it succeeded
        (or there was nothing left to do)."""
        # Phase durations of this task. The V2 worker adds its own phases and
        # publishes the record when NBODY_TIMING_DETAIL is set (task_timing).
        # The package metrics are read from it as well.
        self._timer = PhaseTimer('task')
        self._task_bodies = 0
        self._task_completed = False
        self._report_status(busy=True, job_id=task.job_id)
        try:
            return self._compute_task(task)
        finally:
            self._report_status(busy=False)
            self._observe_package(task)

    def _report_status(self, busy: bool, job_id: int = 0) -> None:
        try:
            worker_metrics.set_worker_status(self.metrics_worker_type, self.compute.name, busy, job_id)
        except Exception as e:  # noqa: BLE001 — metrics must never fail a task
            logger.debug(f"Could not update the worker status metric: {e}")

    def _observe_package(self, task: TaskMessage) -> None:
        """Package metrics of a task that got past the skip check (it started
        loading the state): durations from its PhaseTimer, success only once
        its result was handled."""
        timer = self._timer
        phases = timer.phases
        if 'load_state_total' not in phases:
            return      # skipped, or dropped before any work
        try:
            compute_type = self.compute.name
            # a label from the task: keep its values bounded
            algorithm = task.algorithm if task.algorithm in ('direct', 'barnes_hut') else 'other'
            worker_metrics.observe_package_processing(
                self.metrics_worker_type, compute_type, algorithm,
                duration=timer.elapsed(),
                physics_duration=phases.get('compute'),
                num_bodies=self._task_bodies or None,
                success=self._task_completed,
            )
            worker_metrics.observe_io_breakdown(
                self.metrics_worker_type, compute_type,
                download_time=phases['load_state_total'] + phases.get('load_accel', 0.0),
                upload_time=phases.get('upload', phases.get('register')),
                db_time=phases.get('db'),
            )
        except Exception as e:  # noqa: BLE001 — metrics must never fail a task
            logger.debug(f"Could not record package metrics: {e}")

    def _compute_task(self, task: TaskMessage) -> bool:
        """Load, compute and hand on one package (the body of _process_task)."""
        self._last_failure_transient = False
        start_time = time.perf_counter()
        physics_time = 0.0
        timer = self._timer
        self._on_task_start(task)

        logger.debug(
            f"Processing job={task.job_id} epoch={task.epoch_num} "
            f"pkg={task.package_num}/{task.total_packages}"
        )

        try:
            # 0. Drop tasks that can no longer register (stale generation or
            # epoch, job cancelled) before spending compute on them.
            if self._should_skip(task):
                return True
            self._before_compute(task)

            # 1. Load body data
            download_start = time.perf_counter()
            with timer.phase('load_state_total'):
                all_positions, all_velocities, all_masses, all_ids = self._load_bodies(task)
            download_time = time.perf_counter() - download_start

            n_bodies = len(all_masses)
            logger.debug(f"Loaded {n_bodies} bodies in {download_time:.3f}s")

            if n_bodies == 0:
                logger.error("No bodies loaded")
                return False

            # 1b. Epoch-start accelerations a0 for the accel predictor
            # (returned or shipped). None -> the compute backend recomputes a0 locally when the
            # integration method needs it (e.g. epoch 1, or a linear/frozen job).
            with timer.phase('load_accel'):
                all_accelerations = self._load_accelerations(task, n_bodies)

            # 2. Calculate package bounds
            # V2 sends pre-computed start_idx/end_idx. V1 leaves them -1 so we compute from package_size
            if task.start_idx >= 0 and task.end_idx >= 0:
                start_idx = task.start_idx
                end_idx = min(task.end_idx, n_bodies)
            else:
                start_idx = (task.package_num - 1) * task.package_size
                end_idx = min(task.package_num * task.package_size, n_bodies)

            if start_idx >= n_bodies or end_idx <= start_idx:
                logger.error(f"Invalid package bounds: {start_idx}:{end_idx} for {n_bodies} bodies")
                return False

            self._task_bodies = end_idx - start_idx

            # 3. Extract package slice
            k_positions = all_positions[start_idx:end_idx].copy()
            k_velocities = all_velocities[start_idx:end_idx].copy()
            k_masses = all_masses[start_idx:end_idx].copy()
            k_ids = all_ids[start_idx:end_idx].copy()

            logger.debug(f"Processing {len(k_ids)} bodies (indices {start_idx}:{end_idx})")

            # 4. Compute physics
            physics_start = time.perf_counter()
            with timer.phase('compute'):
                new_k_positions, new_k_velocities, new_k_accel = self.compute.compute_package_step(
                    k_positions=k_positions,
                    k_velocities=k_velocities,
                    k_masses=k_masses,
                    k_ids=k_ids,
                    all_positions=all_positions,
                    all_velocities=all_velocities,
                    all_masses=all_masses,
                    all_ids=all_ids,
                    time_step=task.time_step,
                    algorithm=task.algorithm,
                    integration_method=task.integration_method,
                    barnes_hut_theta=task.barnes_hut_theta,
                    softening_sq=task.softening_sq,
                    all_accelerations=all_accelerations,
                )
            physics_time = time.perf_counter() - physics_start

            logger.debug(f"Physics computation completed in {physics_time:.3f}s")

            # A non-finite result must not be registered: on the last epoch
            # nothing decodes it again, and the job would complete with NaN in
            # its final state.
            # Deterministic, so redelivery cannot help: fail the job.
            if not (np.isfinite(new_k_positions).all() and np.isfinite(new_k_velocities).all()
                    and (new_k_accel is None or np.isfinite(new_k_accel).all())):
                raise ComputeError(self.compute.name,
                                   f"non-finite state computed for epoch {task.epoch_num}, "
                                   f"package {task.package_num}", recoverable=False)

            # 5. Compute local a_max for adaptive timestepping. The backend
            # already returned the last-stage accelerations, so we don't have
            # to do another force evaluation here.
            local_a_max = 0.0
            if task.adaptive_enabled:
                local_a_max = float(np.max(np.sqrt(np.sum(new_k_accel**2, axis=1))))

            # 6. Handle results
            if self._result_handler:
                self._result_handler(
                    task=task,
                    new_positions=new_k_positions,
                    new_velocities=new_k_velocities,
                    masses=k_masses,
                    ids=k_ids,
                    start_idx=start_idx,
                    local_a_max=local_a_max,
                    accelerations=new_k_accel,
                )

            self._task_completed = True
            total_time = time.perf_counter() - start_time
            logger.debug(
                f"Package {task.package_num} completed in {total_time:.3f}s "
                f"(physics: {physics_time:.3f}s)"
            )
            return True

        except ComputeError as e:
            logger.error(f"Compute error: {e}")
            if e.restart_worker:
                self._restart_reason = str(e)
            if e.recoverable:
                # Leave the task pending: it is redelivered to another worker.
                # (Acknowledging it without a partial would leave the epoch
                # incomplete forever.)
                return False
            # acknowledged once the job is failed. Pending if that failed
            return self._fail_job(task, f"Non-recoverable compute error: {e}")

        except Exception as e:
            logger.error(f"Error processing task: {e}", exc_info=True)
            self._last_failure_transient = is_transient_error(e)
            return False

    def _should_skip(self, task: TaskMessage) -> bool:
        """Return True to acknowledge a task without processing it."""
        return False

    @staticmethod
    def _job_is_gone(job_id: int) -> bool:
        """True only if PostgreSQL positively says the job does not exist. A
        database error keeps the task pending instead of dropping it."""
        try:
            return Job.get_or_none(Job.job_id == job_id) is None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not check job {job_id} in PostgreSQL ({e}), keeping the task")
            return False

    def _on_idle(self) -> None:
        """Called on every loop iteration. Subclasses rate-limit their own work."""
        return None

    def _before_compute(self, task: TaskMessage) -> None:
        """Called for every task that will be computed."""
        return None

    def _on_task_start(self, task: TaskMessage) -> None:
        """Called first thing for every claimed task."""
        return None

    def _fail_job(self, task: TaskMessage, reason: str) -> bool:
        """Mark a job FAILED after an error that retrying cannot fix (never
        over a final state such as CANCELLED or COMPLETED). Returns False if
        the failure could not be recorded, so the caller keeps the task."""
        logger.error(f"Failing job {task.job_id}: {reason}")
        try:
            final = (JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value)
            Job.update(
                status=JobStatus.FAILED.value,
                updated_at=datetime.now(timezone.utc),
            ).where((Job.job_id == task.job_id) & (Job.status.not_in(final))).execute()
            return True
        except Exception as e:
            logger.error(f"Could not mark job {task.job_id} FAILED in PostgreSQL: {e}")
            return False

    def _load_bodies(self, task: TaskMessage):
        """(positions, velocities, masses, ids) of the task's bodies, a hook for
        each transport's storage (S3, Redis)."""
        bodies = get_n_bodies(task.bucket, task.file_key)
        return bodies_to_numpy(bodies)

    def _load_accelerations(self, task: TaskMessage, n_bodies: int):
        """Epoch-start accelerations a0 of ALL bodies, (n_bodies, 3) float64, if
        the transport ships them. None (here) makes the compute backend recompute a0
        locally whenever the integration method needs it. The V2 worker overrides
        this to read the merger-shipped payload from Redis."""
        return None

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        logger.debug("Signal handlers registered")

    def _handle_signal(self, signum, frame) -> None:
        """Handle shutdown signal: only set the flag. The loop exits at its next
        check, after any task it holds, within CLAIM_BLOCK_MS when idle.

        Exiting from the handler could abandon a task: a signal arriving just
        after XREADGROUP delivered a message, or before _processing_task is
        set, would leave the task in the dead consumer's pending list until the
        stale-task reclaim (STALE_TASK_MS), stalling the epoch whenever the
        worker pool is scaled down during a job."""
        signal_name = signal.Signals(signum).name
        self._shutdown_requested = True
        busy = self._processing_task
        logger.info(f"Received {signal_name}, shutting down "
                    f"{'after the current task' if busy else 'at the next loop check'}")

    def _cleanup(self) -> None:
        """Cleanup resources."""
        logger.info("Cleaning up...")

        try:
            self.messages.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting message backend: {e}")

        try:
            self.compute.cleanup()
        except Exception as e:
            logger.warning(f"Error cleaning up compute backend: {e}")

        logger.info(f"Worker {self.worker_name} shutdown complete")


class DistributedWorkerV1(DistributedWorker):
    """V1 Distributed Worker with RabbitMQ-specific result handling.

    After computing physics, this worker:
    1. Uploads partial results to S3
    2. Updates package status in PostgreSQL
    3. Notifies the merger via RabbitMQ
    """

    def __init__(
        self,
        compute_backend: ComputeBackend,
        message_backend: MessageBackend,
        worker_name: Optional[str] = None,
    ):
        super().__init__(compute_backend, message_backend, worker_name)

        ensure_buckets_exist()
        self._partial_bucket = PARTIAL_RESULTS_BUCKET_NAME

    def _before_compute(self, task: TaskMessage) -> None:
        """Record the job's start time when an epoch-1 task begins (only the
        first such call writes, the dashboard's run time starts there)."""
        if task.epoch_num == 1:
            mark_job_started(task.job_id)

    def run(self, queue_name: str, warmup: bool = True) -> None:
        """Run V1 worker with built-in result handler."""
        super().run(
            queue_name=queue_name,
            result_handler=self._handle_v1_results,
            warmup=warmup,
        )

    def _handle_v1_results(
        self,
        task: TaskMessage,
        new_positions: np.ndarray,
        new_velocities: np.ndarray,
        masses: np.ndarray,
        ids: np.ndarray,
        start_idx: int,
        local_a_max: float = 0.0,
        accelerations: Optional[np.ndarray] = None,
    ) -> None:
        """Handle results V1 style: upload to S3, update DB, notify merger."""

        # Idempotency: on redelivery (crash after processing, before ack)
        # this package may already be done. Its result is not computed or
        # uploaded again. But if it still waits to be merged, the merger
        # notification is sent again: the first one may be what the crash
        # lost (between the status update and the publish), and the merger
        # tolerates duplicates (it counts the epoch's TO_MERGE rows).
        timer = self._timer or PhaseTimer('task')      # (feeds the package metrics)
        if task.package_id:
            with timer.phase('db'):
                existing = Package.get_or_none(Package.package_id == task.package_id)
            if existing and existing.status in (
                PackageStatus.TO_MERGE.value, PackageStatus.MERGED.value
            ):
                logger.info(
                    f"Package {task.package_id} (job {task.job_id}, epoch {task.epoch_num}) "
                    f"already {existing.status}, skipping duplicate result handling"
                )
                if existing.status == PackageStatus.TO_MERGE.value and existing.file_key:
                    self._notify_merger(task, existing.file_key, local_a_max)
                return

        bodies = []
        for i in range(len(ids)):
            bodies.append({
                'n': start_idx + i + 1,
                'idBody': int(ids[i]),
                'm': float(masses[i]),
                'x': float(new_positions[i, 0]),
                'y': float(new_positions[i, 1]),
                'z': float(new_positions[i, 2]),
                'vx': float(new_velocities[i, 0]),
                'vy': float(new_velocities[i, 1]),
                'vz': float(new_velocities[i, 2]),
            })

        # Upload to S3
        csv_content = get_partial_results_file(bodies)
        file_key = (
            f'job_id/{task.job_id}/epoch_num/{task.epoch_num}/'
            f'package_num/{task.package_num}/partial_results.csv'
        )
        with timer.phase('upload'):
            upload_file(self._partial_bucket, file_key, io.StringIO(csv_content))
        logger.debug(f"Uploaded partial results to {file_key}")

        # Update package in DB: only from IN_PROGRESS, so a duplicate delivery
        # finishing after the epoch merged cannot turn MERGED back into
        # TO_MERGE (and trigger a second merge). The first one has notified
        if task.package_id:
            with timer.phase('db'):
                updated = Package.update(file_key=file_key, status=PackageStatus.TO_MERGE.value).where(
                    (Package.package_id == task.package_id)
                    & (Package.status == PackageStatus.IN_PROGRESS.value)).execute()
            if not updated:
                logger.info(f"Package {task.package_id} (job {task.job_id}, epoch {task.epoch_num}) "
                            f"was completed by another delivery, not notifying the merger")
                return

        self._notify_merger(task, file_key, local_a_max)

    def _notify_merger(self, task: TaskMessage, file_key: str, local_a_max: float) -> None:
        """Tell the v1 merger a package of the epoch is ready (persistent, so a
        broker restart does not lose it)."""
        if isinstance(self.messages, RabbitMQBackend):
            merger_msg = MergerMessage(
                job_id=task.job_id,
                bucket=self._partial_bucket,
                file_key=file_key,
                epoch_num=task.epoch_num,
                package_num=task.package_num,
                package_id=task.package_id,
                package_size=task.package_size,
                local_a_max=local_a_max,
            )
            channel = self.messages.get_channel()
            channel.basic_publish(
                exchange='',
                routing_key=TO_MERGE_QUEUE,
                body=merger_msg.to_json(),
                properties=pika.BasicProperties(delivery_mode=2),
            )
