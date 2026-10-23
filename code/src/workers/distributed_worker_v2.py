# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The V2 distributed worker: DistributedWorkerV2 takes package tasks from
Redis Streams, registers each result in its epoch's staging key, and the last
registrant commits and publishes the epoch (merge lease, takeover of a dead
merger, generation fencing). The entry points are v2/worker_v2.py and
v2/worker_v2_gpu.py."""

import concurrent.futures
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np

import prometheus_metrics
import task_timing
from barnes_hut import compute_accelerations_barnes_hut
from constants import G_CONST
from integrators import get_predictor_order
from job_timestamps import mark_job_started, record_compute_hosts
from models import Job
from physics import compute_accelerations, estimate_adaptive_dt_from_amax
from schemas import JobStatus
from task_timing import PhaseTimer
from utils.metadata import compute_host
from v2.binary_format import (
    BODY_SIZE,
    accelerations_to_binary,
    binary_to_accelerations,
    binary_to_numpy,
    numpy_to_binary,
)
from v2.epoch_tasks import EpochParams, epoch_task_template
from v2.file_service_v2 import upload_final_results_csv, upload_snapshot_async
from v2.orchestrator_v2 import RecoveryConflict
from v2.recovery import recover_v2_job
from v2.redis_service import (
    GPU_TASKS_STREAM,
    JOB_PAUSED,
    MERGE_LEASE_MS,
    PARTIAL_REJECTED,
    STAGING_INCOMPLETE,
    TASKS_STREAM,
    FenceState,
    rt_count,
    rt_reset,
)
from v2.schemas_v2 import JobStatusV2
from workers.backends.redis_backend import RedisBackend
from workers.base.compute_backend import ComputeBackend
from workers.base.message_backend import MessageBackend, TaskMessage
from workers.distributed_worker import DistributedWorker, logger
from workers.fault_state import FROZEN
from workers.renew_loop import RenewLoop


# How often (in epochs) a merger trims the task stream of entries no consumer
# group needs (RedisService.prune_task_stream). Idle workers trim it too.
TASK_STREAM_PRUNE_INTERVAL_EPOCHS = 50


# How long finalization waits for the final epoch's snapshot upload (which
# retries internally). Past it the job completes anyway and the upload goes on.
FINAL_SNAPSHOT_TIMEOUT_S = 120


# Older names of two crash points, still accepted (see fault_point)
FAULT_POINT_ALIASES = {'before_enqueue': 'before_publish', 'after_enqueue': 'after_publish'}


@dataclass(frozen=True)
class _StagedState:
    """A commit: the state was renamed from its staging key, so the merger
    holds only its size and whether the commit also installed a(0)."""
    n_bytes: int
    accels_installed: bool


def fault_point(point: str, job_id: int, epoch: int, worker_name: str, redis_service) -> None:
    """Fault injection for testing fault tolerance (off unless NBODY_FAULT_INJECT is set, never set it in production).

    NBODY_FAULT_INJECT="point@epoch[@job][@freeze=S][,...]" makes the first
    worker that reaches `point` for `epoch` of any job exit immediately with
    status 137, as if SIGKILLed: no finally blocks, no lease release, no ack.
    With "@freeze=S" it instead freezes for S seconds, as if SIGSTOPped and
    continued: the main thread sleeps and the lease renewer and task heartbeat
    pause (workers.fault_state.FROZEN), then the worker carries on as a zombie
    whose lease has expired. A Redis SET NX per (job, point, epoch) ensures one
    fault per point, even though replacement pods inherit the environment.
    Points: mid_task (computed, not registered), before_commit (merge lease
    held), after_commit, before_publish (committed, the epoch's cleanup, the
    next epoch's enqueue, the end of publication and the lease release, one
    script for a non-final epoch, still to do), after_publish (all done, on the
    final epoch: finalized, still in the pending set). before_enqueue and
    after_enqueue are accepted as aliases (FAULT_POINT_ALIASES).
    """
    spec = os.environ.get('NBODY_FAULT_INJECT', '').strip()
    if not spec:
        return
    for item in spec.split(','):
        parts = item.strip().split('@')
        named = parts[0]            # as written (the fault key keeps the spelling given)
        if len(parts) < 2 or FAULT_POINT_ALIASES.get(named, named) != point \
                or not parts[1].isdigit() or int(parts[1]) != epoch:
            continue
        opts = parts[2:]
        scoped = [p for p in opts if p.isdigit()]
        if scoped and int(scoped[0]) != job_id:
            continue   # scoped to another job
        freeze_s = next((float(p.split('=', 1)[1]) for p in opts if p.startswith('freeze=')), None)
        key = f'v2:fault:{job_id}:{named}:{epoch}'
        if not redis_service.client.set(key, f'{worker_name}|{time.time():.3f}', nx=True, ex=7 * 24 * 3600):
            continue
        if freeze_s is not None:
            logger.critical(f"FAULT INJECTION: {worker_name} freezes for {freeze_s:.0f} s at {point} "
                            f"(job {job_id}, epoch {epoch})")
            FROZEN.set()
            try:
                time.sleep(freeze_s)
            finally:
                FROZEN.clear()
            logger.critical(f"FAULT INJECTION: {worker_name} resumes after the freeze at {point} "
                            f"(job {job_id}, epoch {epoch})")
            return
        logger.critical(f"FAULT INJECTION: {worker_name} exits at {point} "
                        f"(job {job_id}, epoch {epoch})")
        for handler in logger.handlers + logging.getLogger().handlers:
            handler.flush()
        os._exit(137)


def task_timing_diagnostics_enabled() -> bool:
    return task_timing.DIAGNOSTICS_ENABLED


def _fence(redis_service, job_id: int):
    """The job's fencing fields as a v2.redis_service.FenceState."""
    return FenceState.of(redis_service.get_fence_state(job_id))


class DistributedWorkerV2(DistributedWorker):
    """V2 Distributed Worker with Redis-specific result handling.

    After computing physics, this worker:
    1. Stores partial results in Redis (binary format)
    2. Counts the package towards its epoch
    3. If last package, merges and advances epoch
    """

    metrics_worker_type = 'distributed_v2'

    def __init__(
        self,
        compute_backend: ComputeBackend,
        message_backend: MessageBackend,
        worker_name: Optional[str] = None,
    ):
        if not isinstance(message_backend, RedisBackend):
            # the epoch state, fences and merges all live in Redis
            raise TypeError("DistributedWorkerV2 requires the Redis message backend")
        super().__init__(compute_backend, message_backend, worker_name)
        self._hosts_noted = set()           # jobs whose compute host was recorded
        self._last_takeover_scan = float('-inf')
        self._last_consumer_cleanup = float('-inf')

    def run(self, queue_name: str, warmup: bool = True) -> None:
        """Run V2 worker with built-in result handler."""
        super().run(
            queue_name=queue_name,
            result_handler=self._handle_v2_results,
            warmup=warmup,
        )

    def _prefetched_for(self, task: TaskMessage) -> Optional[dict]:
        """The reads _should_skip made for this task (fence, state, a(0)), or
        None if it made none (a test calling the loaders directly, a failed
        pre-check)."""
        pre = getattr(self, '_prefetched', None)
        if pre and pre['job_id'] == task.job_id and pre['epoch'] == task.epoch_num:
            return pre
        return None

    def _load_bodies(self, task: TaskMessage):
        """Load bodies from Redis binary format (read by _should_skip's
        pipeline when it ran, on its own otherwise)."""

        redis_service = self.messages.get_redis_service()
        timer = self._task_timer()
        pre = self._prefetched_for(task)
        if pre is not None and pre['bodies'] is not None:
            bodies_binary = pre['bodies']
            pre['bodies'] = None            # each state is decoded once
        else:
            with timer.phase('fetch_state'):
                bodies_binary = redis_service.get_bodies(task.job_id)

        if bodies_binary is None:
            raise ValueError(f"No body data found for job {task.job_id}")

        timer.add('bytes_state_in', len(bodies_binary))
        with timer.phase('decode_state'):
            # No NaN/Inf scan of the whole state per task: it is the validated
            # input, a validated snapshot, or a join of package results each
            # checked finite before registration (_process_task)
            return binary_to_numpy(bodies_binary, validate=False)

    def _load_accelerations(self, task: TaskMessage, n_bodies: int):
        """Read the published epoch-start accelerations from Redis.

        Returns None — so the backend recomputes a0 locally — when the payload is
        absent (epoch 1 before the first merge, or a linear/frozen job that never
        ships it) or size-mismatched (stale). Consistent with the bodies key:
        both are written together by the merger.
        """
        if get_predictor_order(task.integration_method) < 2:
            return None  # never shipped for these methods: skip the round trip

        pre = self._prefetched_for(task)
        if pre is not None and pre['want_accels']:
            accel_binary = pre['accels']
            self._prefetched = None
        else:
            accel_binary = self.messages.get_redis_service().get_accelerations(task.job_id)
        if not accel_binary:
            return None
        self._task_timer().add('bytes_accel_in', len(accel_binary))
        try:
            return binary_to_accelerations(accel_binary, n_bodies)
        except ValueError as e:
            logger.warning(
                f"Ignoring shipped accelerations for job {task.job_id} "
                f"(falling back to local recompute): {e}"
            )
            return None

    # Epoch fencing
    #
    # The epoch transition is built from atomic Redis operations (Lua scripts
    # in v2/redis_service.py):
    #   register_partial   the package's result into its slice of the staging
    #                      key, a_max, counted marker, and the pending-merges
    #                      entry once all packages are in
    #   begin_merge        owner-tokened merge lease (renewed while the merge
    #                      runs) and the fence, in one round trip
    #   commit_staged      compare-and-set of committed_epoch. The staging key
    #                      is renamed into the state
    #   publish_epoch      cleanup, the next epoch's tasks (once per
    #                      generation), end of publication and lease release
    # Everything after the commit is idempotent, and the epoch stays in the
    # pending-merges set until publication finishes, so any worker can complete
    # the epoch if the merger dies at any point (_try_takeover).

    # Merge lease length and takeover pacing. Tests shorten them.
    merge_lease_ms: Optional[int] = None
    takeover_interval_s: float = 5.0
    takeover_grace_s: float = 2.0

    def _should_skip(self, task: TaskMessage) -> bool:
        """Skip tasks that the registration fence would reject anyway.

        The fence is read together with the state and, for the acceleration
        predictor, a(0), in one pipeline (get_task_inputs): one round trip for
        all a task reads. The loaders use what it read. The timing record's
        fetch_state phase covers the whole pipeline."""
        self._prefetched = None
        want_accels = get_predictor_order(task.integration_method) >= 2
        redis_service = self.messages.get_redis_service()
        timer = self._task_timer()
        try:
            with timer.phase('fetch_state'):
                inputs = None
                if task.package_num % 2 == 1:
                    # odd packages read the state from a replica when replica
                    # reads are on. None: not on, or not provably this epoch's
                    # state, so the primary is read
                    inputs = redis_service.get_task_inputs_from_replica(
                        task.job_id, task.generation, task.epoch_num, want_accels)
                    if inputs is not None:
                        timer.add('replica_reads', 1)
                    elif getattr(redis_service, 'replica_client', None) is not None:
                        timer.add('replica_fallbacks', 1)
                if inputs is None:
                    inputs = redis_service.get_task_inputs(task.job_id, want_accels)
                fields, bodies, accels = inputs
        except Exception as e:  # noqa: BLE001 — the fence in register_partial still applies
            logger.debug(f"Fence pre-check failed for job {task.job_id}: {e}")
            return False
        self._prefetched = {'job_id': task.job_id, 'epoch': task.epoch_num, 'bodies': bodies,
                            'accels': accels, 'want_accels': want_accels}
        fence = FenceState.of(fields)
        if not fence.is_current(task.generation):
            logger.info(f"Skipping task of job {task.job_id} epoch {task.epoch_num}: "
                        f"generation {task.generation} is stale (now {fence.generation})")
            return True
        if fence.status is None:
            logger.info(f"Skipping task of job {task.job_id} epoch {task.epoch_num}: job meta is gone")
            return True
        if fence.committed is not None and task.epoch_num != fence.committed + 1:
            logger.info(f"Skipping task of job {task.job_id} epoch {task.epoch_num}: "
                        f"committed epoch is {fence.committed_epoch}")
            return True
        if fence.status in (JobStatusV2.CANCELLED.value, JobStatusV2.FAILED.value, JobStatusV2.COMPLETED.value):
            logger.info(f"Skipping task of job {task.job_id}: job is {fence.status}")
            return True
        return False

    def _task_timer(self):
        """The PhaseTimer of the current (or last) task. A throwaway one
        before the first task."""
        return self._timer if self._timer is not None else PhaseTimer('task')

    def _on_task_start(self, task: TaskMessage) -> None:
        """Start counting the task's Redis round trips (from the fence
        pre-check on, the claim and the ack happen outside the record)."""
        rt_reset()
        self._prefetched = None

    def _before_compute(self, task: TaskMessage) -> None:
        """Record this worker's host for the job (once per job), and the job's
        start time when an epoch-1 task begins."""
        noted = self._hosts_noted
        if task.job_id not in noted:
            try:
                self.messages.get_redis_service().add_compute_host(task.job_id, compute_host())
                noted.add(task.job_id)
            except Exception as e:  # noqa: BLE001 — bookkeeping only
                logger.warning(f"Could not record the host of job {task.job_id}: {e}")
        if task.epoch_num != 1:
            return
        try:
            self.messages.get_redis_service().mark_started(task.job_id)
            # conditional on started_at IS NULL, so every epoch-1 task may try:
            # a transient database error on the first one is then not final
            mark_job_started(task.job_id)
        except Exception as e:  # noqa: BLE001 — bookkeeping only
            logger.warning(f"Could not record start of job {task.job_id}: {e}")

    def _recover_lost_epoch(self, task: TaskMessage, reason: str) -> bool:
        """Recover a job whose epoch lost its staged state, from its latest
        snapshot under a new generation (as the API's recover endpoint does).
        Only a job still at the task's generation: a second merger that found
        the same loss leaves the job to the first one's recovery. The job is
        failed only if no recovery can run. Returns False if neither could be
        recorded, so the caller keeps the pending merge for another try."""
        try:
            pg_job = Job.get_or_none(Job.job_id == task.job_id)
            if pg_job is not None:
                recovered = recover_v2_job(task.job_id, pg_job, if_generation=task.generation)
                if recovered is not None:
                    logger.warning(f"{reason}: job {task.job_id} recovered from epoch {recovered}")
                    return True
                logger.error(f"{reason}: job {task.job_id} has nothing to recover from")
        except RecoveryConflict as e:
            # recovered by another worker, or cancelled meanwhile
            logger.info(f"{reason}: {e}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"{reason}: recovery of job {task.job_id} failed ({e}), failing the job")
        return self._fail_job(task, reason)

    def _fail_job(self, task: TaskMessage, reason: str) -> bool:
        """Fail the job in Redis first, fenced on the task's generation. Only
        if that is accepted in PostgreSQL too. A leftover task from before a
        recovery, or of a cancelled job, is thus refused instead of failing the
        job. If Redis cannot be reached the job is failed nowhere (the workers
        would go on running it while PostgreSQL said FAILED) and False is
        returned: the caller leaves the task pending, to be retried."""
        try:
            result = self.messages.get_redis_service().fail_job(task.job_id, reason, task.generation)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Could not mark job {task.job_id} FAILED in Redis ({e}), not failing it "
                         f"in PostgreSQL either, the task stays pending: {reason}")
            return False
        if result < 0:
            try:
                fence = _fence(self.messages.get_redis_service(), task.job_id)
            except Exception as e:  # noqa: BLE001 — the caller keeps the task, retried later
                logger.error(f"Could not read the fence of job {task.job_id} after a refused "
                             f"fail ({e}), the task stays pending: {reason}")
                return False
            if fence.status == JobStatusV2.FAILED.value and fence.is_current(task.generation):
                # FAILED already (possibly by this call's own write, retried
                # after a lost reply): make sure PostgreSQL says so too
                super()._fail_job(task, reason)
                return True
            logger.info(f"Not failing job {task.job_id} (code {result}: stale task or "
                        f"job not running): {reason}")
            return True
        # Redis (which the workers follow) has it FAILED. A PostgreSQL error is
        # logged by the base class and does not keep the task.
        super()._fail_job(task, reason)
        return True

    def _handle_v2_results(
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
        """Register this package's result. Merge if it completes the epoch."""

        redis_service = self.messages.get_redis_service()
        timer = self._task_timer()

        with timer.phase('encode'):
            result_binary = numpy_to_binary(
                new_positions,
                new_velocities,
                masses,
                ids,
                start_idx=start_idx,
            )
            # Acceleration-predictor methods: the accelerations this package
            # returned become its rows of the next epoch's a(0) (see
            # _maybe_ship_accelerations), replacing a serial O(N^2) pass.
            accel_binary = None
            if accelerations is not None and get_predictor_order(task.integration_method) >= 2:
                accel_binary = accelerations_to_binary(accelerations)
        timer.add('bytes_out', len(result_binary) + (len(accel_binary) if accel_binary else 0))

        fault_point('mid_task', task.job_id, task.epoch_num, self.worker_name, redis_service)
        with timer.phase('register'):
            done_count = redis_service.register_partial(
                job_id=task.job_id,
                generation=task.generation,
                epoch=task.epoch_num,
                package_num=task.package_num,
                total_packages=task.total_packages,
                data=result_binary,
                a_max=local_a_max if task.adaptive_enabled else None,
                accelerations=accel_binary,
                package_size=task.package_size,
            )
        self._publish_timing(task, timer, redis_service, package_num=task.package_num,
                             n_local=len(ids), registered=done_count)
        if done_count == PARTIAL_REJECTED:
            # a result that does not fit its package fails the same way on
            # every retry
            self._fail_job(task, f"Epoch {task.epoch_num}: result of package {task.package_num} "
                                 f"({len(result_binary)} bytes) does not fit the package")
            return
        if done_count < 0:
            logger.info(
                f"Result of job {task.job_id} epoch {task.epoch_num} pkg {task.package_num} "
                f"rejected by the fence (code {done_count}), dropping it"
            )
            return
        logger.debug(f"Package {task.package_num} done, count={done_count}/{task.total_packages}")

        if done_count >= task.total_packages:
            try:
                self._merge_and_advance(task, redis_service)
            except Exception as e:  # noqa: BLE001
                # This task's result is registered: failing the task would
                # leave it pending and recompute it after the stale threshold.
                # The epoch stays in the pending-merges set (its lease is
                # released by the merge's finally), so a takeover finishes it.
                logger.error(f"Merge of job {task.job_id} epoch {task.epoch_num} failed, "
                             f"left to a takeover: {e}", exc_info=True)

    def _publish_timing(self, task: TaskMessage, timer, redis_service, **fields) -> None:
        """Append a timing record to v2:timings:{job} if NBODY_TIMING_DETAIL is set."""
        if not task_timing.TIMING_ENABLED:
            return
        try:
            record = timer.record(
                job=task.job_id, gen=task.generation, epoch=task.epoch_num,
                worker=self.worker_name, P=task.total_packages, redis_rt=rt_count(), **fields,
            )
            redis_service.push_timing(task.job_id, record)
        except Exception as e:  # noqa: BLE001 — instrumentation must never fail a task
            logger.warning(f"Could not publish timing record for job {task.job_id}: {e}")

    def _next_dt(self, task: TaskMessage, redis_service, amax_values=None) -> float:
        """Step size of the next epoch (adaptive: from the packages' a_max,
        amax_values as begin_merge read them, else read here)."""
        next_dt = task.time_step
        if task.adaptive_enabled:
            if amax_values is None:
                amax_values = redis_service.get_epoch_amax_values(task.job_id, task.epoch_num,
                                                                  task.generation)
            if amax_values:
                # also for a_max == 0: the estimate then gives dt_max, as the
                # monolithic estimate_adaptive_dt does, so both engines take the
                # same steps. Non-finite accelerations are refused before a
                # result is registered, so only an overflow to inf reaches here.
                # np.max propagates a NaN (Python's max may skip one), which the
                # estimate turns into dt_min
                global_a_max = float(np.max(np.asarray(amax_values, dtype=np.float64)))
                next_dt = estimate_adaptive_dt_from_amax(
                    global_a_max, task.time_step,
                    eta=task.adaptive_eta,
                    softening=task.softening_sq ** 0.5,
                    dt_min=task.adaptive_dt_min,
                    dt_max=task.adaptive_dt_max,
                )
                logger.debug(f"Adaptive dt: a_max={global_a_max:.4e} -> next_dt={next_dt:.4e}")
        return next_dt

    def _maybe_ship_accelerations(self, task: TaskMessage, merged: Optional[bytes], redis_service,
                                  absent: bool = False, state=None) -> None:
        """Compute and ship the epoch-start accelerations a(0) of the committed
        state for acceleration-predictor methods, so workers don't each
        recompute them. Called when the commit did not install the
        accelerations the packages returned (see _COMMIT_STAGED), i.e. when
        they were incomplete (source 'fallback'). Idempotent: a payload
        written by an earlier merger of this epoch is kept (the commit dropped
        the previous epoch's). Non-fatal: on failure workers fall back to
        recomputing a(0) locally.

        absent: the merge that committed the epoch knows its commit dropped
        the payload, saving the read. state: a reader of the committed state
        when `merged` is None, called only here, where a(0) is computed.
        """

        if get_predictor_order(task.integration_method) < 2:
            return
        try:
            job_id, gen, epoch = task.job_id, task.generation, task.epoch_num
            if not absent and redis_service.has_accelerations(job_id):
                return  # already shipped for this state (the commit drops older payloads)
            source = 'fallback'
            logger.warning(f"Returned accelerations incomplete for job {job_id} "
                           f"epoch {epoch}, recomputing a(0)")
            if merged is None and state is not None:
                merged = state()
            if merged is None or not _fence(redis_service, job_id).holds_state_of(gen, epoch):
                return  # state moved on. Skip the O(N^2) pass
            positions, _vel, masses, ids = binary_to_numpy(merged, validate=False)
            if task.algorithm == 'barnes_hut':
                a0 = compute_accelerations_barnes_hut(
                    positions, masses, ids,
                    task.barnes_hut_theta, G_CONST, task.softening_sq,
                )
            else:
                a0 = compute_accelerations(
                    positions, masses, ids, softening_sq=task.softening_sq,
                )
            blob = accelerations_to_binary(a0)
            # Fenced: written only while this epoch's state is the committed one
            # of the live generation, so a publisher that lost its lease cannot
            # attach a(0) of an older state to a newer one.
            result = redis_service.ship_accelerations(job_id, gen, epoch, blob, source)
            if result < 0:
                logger.info(f"a(0) of job {job_id} epoch {epoch} not shipped: state moved on")
        except Exception as e:  # noqa: BLE001 — shipping is an optimization, never fatal
            logger.warning(
                f"Failed to ship accelerations for job {task.job_id} "
                f"epoch {task.epoch_num} (workers will recompute a0): {e}"
            )
            # Nothing to invalidate: the commit deletes the payload in the
            # same script that installs a new state, and recovery deletes it
            # on restore, so any payload present now belongs to the committed
            # state or a newer one. Deleting it here could drop a newer
            # state's accelerations and change how returned-acceleration runs
            # continue.

    def _merge_and_advance(self, task: TaskMessage, redis_service) -> None:
        """Commit an epoch's staged state and publish it.

        Safe to call from several workers and to repeat: the lease admits one
        merger at a time, the commit is compare-and-set, and publication is
        idempotent. A caller that finds the lease held simply returns. If the
        holder dies, the lease expires and a takeover finishes the epoch.
        """

        rt_reset()
        timer = PhaseTimer('merge')
        lease_ms = self.merge_lease_ms or MERGE_LEASE_MS
        job_id, gen, epoch = task.job_id, task.generation, task.epoch_num
        # Lease, fence, previous simulation time and the a_max values in one
        # round trip
        with timer.phase('begin_merge'):
            start = redis_service.begin_merge(
                job_id, gen, epoch, self.worker_name, lease_ms,
                want_amax=bool(task.adaptive_enabled))
        if start is None:
            logger.debug(f"Merge lease for job {job_id} epoch {epoch} held by another worker")
            return
        token = start.token

        with timer.phase('renewer_start'):
            # renewed by the process's lease keeper (no thread per merge)
            renewer = _MERGE_LEASES.hold(redis_service, job_id, gen, epoch, token, lease_ms)
        # What this call did, for its timing record ('merge', 'resume',
        # 'finalize_retry'. None: nothing, and no record)
        path = None
        # set once publish_epoch has released the lease
        state = {'released': False}
        try:
            fence = start.fence
            if not fence.is_current(gen):
                logger.info(f"Merge of job {job_id} epoch {epoch}: generation {gen} is stale")
                redis_service.drop_pending_merge(job_id, gen, epoch)
                return

            merged = next_dt = None
            committed_now = False
            staged_commit = None    # the commit's _StagedState
            if not fence.committed_at_least(epoch):
                path = 'merge'
                committed = self._merge_once(task, redis_service, timer, start)
                if committed is None:
                    return
                staged_commit, next_dt, committed_now = committed
                if staged_commit is None:
                    path = None     # committed by another merger: publish it below

            if staged_commit is None:
                if self._only_finalize_left(task, redis_service):
                    path = 'finalize_retry'
                    if self._finalize_job(task, None, redis_service):
                        redis_service.finish_publication(job_id, gen, epoch)
                    return
                resumed = self._committed_state_for_publication(task, redis_service)
                if resumed is None:
                    return
                merged, next_dt = resumed
                path = 'resume'
                logger.info(f"Resuming publication of job {job_id} epoch {epoch}")

            if renewer.lost:
                logger.warning(f"Lost merge lease for job {job_id} epoch {epoch}, "
                               f"continuing idempotent publication")
            # a commit made by this call dropped any earlier a(0) payload, so
            # none needs checking for
            self._publish_epoch(task, merged, next_dt, redis_service, timer,
                                token=token, renewer=renewer, lease_state=state,
                                accels_absent=committed_now, staged=staged_commit)

        finally:
            with timer.phase('renewer_stop'):
                renewer.stop()
            if not state['released']:
                try:
                    with timer.phase('release'):
                        redis_service.release_merge_lease(job_id, gen, epoch, token)
                except Exception as e:  # noqa: BLE001 — the lease expires on its own
                    logger.warning(f"Could not release merge lease for job {job_id} epoch {epoch}: {e}")
            if path is not None:
                self._publish_timing(task, timer, redis_service, path=path,
                                     takeover=task.message_id == 'takeover')

    def _merge_once(self, task: TaskMessage, redis_service, timer, start):
        """Commit the epoch by renaming its staged state (under the merge
        lease, the epoch not yet committed when begin_merge read the fence,
        `start` is what it read).

        Returns (_StagedState, next dt, committed by this call) once the epoch
        is committed, by this call or concurrently by another merger with the
        same state. (None, None, False) if a merger whose lease lapsed
        committed and cleaned up the epoch first, so its publication is left to
        finish from the committed state. None when nothing is left to do (the
        job was failed, or the commit refused).
        """
        job_id, gen, epoch = task.job_id, task.generation, task.epoch_num
        logger.debug(f"Committing epoch {epoch} for job {job_id}")
        with timer.phase('next_dt'):
            next_dt = self._next_dt(task, redis_service, amax_values=start.amax)
        if epoch == 1 and task_timing_diagnostics_enabled():
            # conserved quantities of the initial state (bodies still holds it)
            self._record_diagnostics(task, 0, redis_service.get_bodies(job_id),
                                     redis_service, timer)
        fault_point('before_commit', job_id, epoch, self.worker_name, redis_service)
        with timer.phase('commit'):
            result, installed, n_bytes = redis_service.commit_staged(
                job_id, gen, epoch, task.total_packages, next_dt,
                sim_time=self._sim_time_after(task, redis_service, prev=start.sim_time),
                epoch_dt=task.time_step,
                install_accelerations=get_predictor_order(task.integration_method) >= 2)
        if result == STAGING_INCOMPLETE:
            # Either a merger whose lease lapsed committed (and cleaned up)
            # the epoch after our fence read, or Redis lost the staged state
            # (a failover that lost writes not yet on the replica), which
            # then cannot be rebuilt: the job restarts from its latest snapshot
            fence = _fence(redis_service, job_id)
            if fence.is_current(gen) and fence.committed_at_least(epoch):
                return None, None, False
            if self._recover_lost_epoch(task, f"Epoch {epoch}: staged state incomplete"):
                redis_service.drop_pending_merge(job_id, gen, epoch)
            return None
        if result < 0:
            logger.info(f"Commit of job {job_id} epoch {epoch} rejected (code {result})")
            redis_service.drop_pending_merge(job_id, gen, epoch)
            return None
        if result == 0:
            logger.warning(f"Epoch {epoch} of job {job_id} was committed concurrently")
        timer.add('bytes_staged', n_bytes)
        return _StagedState(n_bytes, installed and result == 1), next_dt, result == 1

    @staticmethod
    def _only_finalize_left(task: TaskMessage, redis_service) -> bool:
        """True if the job's final epoch is published and Redis has the job
        COMPLETED, so only the PostgreSQL record of the completion is left to
        retry (e.g. database down): Redis is COMPLETED only after the snapshot,
        the cleanup and the results upload. Decided before reading the state,
        which every idle worker would otherwise fetch each takeover tick, and
        which may have expired by now."""
        return (task.epoch_num >= task.num_epochs
                and _fence(redis_service, task.job_id).status == JobStatusV2.COMPLETED.value)

    _UNREAD = object()

    @staticmethod
    def _sim_time_after(task: TaskMessage, redis_service, prev=_UNREAD) -> Optional[float]:
        """Simulation time reached by this epoch: the committed time of the
        previous epoch plus this epoch's step (adaptive runs have no fixed
        step, so epoch * dt is not the time). Summed in the same
        order as the monolithic worker's clock. None when the previous time is
        unknown (a job recovered from a snapshot without one). `prev`: the
        meta's sim_time as begin_merge read it (read here if not given)."""
        if prev is DistributedWorkerV2._UNREAD:
            prev = redis_service.get_job_meta(task.job_id).get('sim_time')
        if prev in (None, ''):
            if task.epoch_num != 1:
                return None
            prev = 0.0
        return float(prev) + task.time_step

    def _committed_state_for_publication(self, task: TaskMessage, redis_service):
        """(merged state, next dt) with which to finish publishing an epoch that
        an earlier merger committed, or None when nothing is left to do.

        The state is read from the bodies key and then checked to still be the
        one committed for this epoch (a slow takeover must not publish, e.g.
        snapshot, a later epoch's state under this epoch's number). A missing
        publish marker means it expired while the epoch was still pending (no
        worker for longer than its TTL): publication is then still due, with
        the next step the commit also recorded in the meta.
        """
        job_id, gen, epoch = task.job_id, task.generation, task.epoch_num
        next_dt = redis_service.get_publish_marker(job_id, gen, epoch)
        merged = redis_service.get_bodies(job_id)
        meta_next_dt = redis_service.get_job_meta(job_id).get('next_dt') if next_dt is None else None
        fence = _fence(redis_service, job_id)
        if not fence.is_current(gen):
            redis_service.drop_pending_merge(job_id, gen, epoch)
            return None
        if fence.committed_is(epoch) and merged is None:
            # committed, but the state is gone (evicted or deleted): nothing
            # can publish this epoch, and taking it over again will not help
            logger.error(f"Job {job_id} epoch {epoch} is committed but its state is missing from "
                         f"Redis, dropping the pending publication. Recover the job from its "
                         f"latest snapshot.")
            redis_service.drop_pending_merge(job_id, gen, epoch)
            return None
        if not fence.committed_is(epoch) or merged is None:
            # the job moved on: the epoch was published and the next committed
            redis_service.finish_publication(job_id, gen, epoch)
            return None
        if next_dt is None:
            next_dt = float(meta_next_dt) if meta_next_dt else task.time_step
            logger.warning(f"Publish marker of job {job_id} epoch {epoch} expired, "
                           f"publishing with next step {next_dt:.4e}")
        return merged, next_dt

    def _record_diagnostics(self, task: TaskMessage, epoch: int, state: Optional[bytes],
                            redis_service, timer) -> None:
        """Store energy/momentum of a state if NBODY_SNAPSHOT_DIAGNOSTICS is set."""
        if not task_timing.DIAGNOSTICS_ENABLED or not state:
            return
        try:
            with timer.phase('diagnostics'):
                values = task_timing.energy_diagnostics(state, task.softening_sq)
                values['dt'] = task.time_step
                redis_service.store_diagnostics(task.job_id, epoch, values)
        except Exception as e:  # noqa: BLE001 — diagnostics must never fail a merge
            logger.warning(f"Could not record diagnostics for job {task.job_id} epoch {epoch}: {e}")

    def _publish_epoch(self, task: TaskMessage, merged: Optional[bytes], next_dt: float,
                       redis_service, timer=None, token: Optional[str] = None, renewer=None,
                       lease_state=None, accels_absent: bool = False,
                       staged: Optional['_StagedState'] = None) -> None:
        """Idempotent steps after a commit. The epoch leaves the pending set last.

        staged: this call committed the epoch: `merged` is None and the state
        is read from Redis only by the steps that need it (snapshot,
        diagnostics, final results, a computed a(0)). Otherwise `merged` is
        the committed state, read back to resume an earlier merger's
        publication.

        token / renewer / lease_state: the caller's merge lease, which the
        publication script of a non-final epoch releases (lease_state
        ['released'] is then set, and the caller does not release it again).
        accels_absent: see _maybe_ship_accelerations."""

        timer = timer or PhaseTimer('merge')
        job_id, gen, epoch = task.job_id, task.generation, task.epoch_num
        n_bodies = staged.n_bytes // BODY_SIZE if staged is not None else len(merged) // BODY_SIZE
        state_moved_on = False

        def state() -> Optional[bytes]:
            # the committed state of this epoch (read here, once). None if a
            # later epoch has committed meanwhile
            nonlocal merged, state_moved_on
            if merged is None and not state_moved_on:
                with timer.phase('fetch_state'):
                    blob = redis_service.get_bodies(job_id)
                    if blob is None or not _fence(redis_service, job_id).holds_state_of(gen, epoch):
                        state_moved_on = True
                    else:
                        merged = blob
                        timer.add('bytes_in', len(blob))
            return merged

        fault_point('after_commit', job_id, epoch, self.worker_name, redis_service)

        # Accelerations of the committed state for acceleration-predictor jobs
        # (the commit dropped the previous epoch's payload, or installed the
        # staged returned rows).
        if not (staged is not None and staged.accels_installed):
            with timer.phase('ship_accel'):
                self._maybe_ship_accelerations(task, merged, redis_service,
                                               absent=accels_absent, state=state)

        # Snapshot every snapshot_interval epochs and of the final state, like the
        # monolithic engine: _finalize_job writes only the final results CSV, so
        # without the final snapshot the animation and the comparison metrics of
        # a v2 job stopped one interval short of the end. An interval of 0 (the
        # API default) means the final snapshot only.
        snapshot_interval = task.snapshot_interval or 0
        is_final = epoch >= task.num_epochs
        if is_final or (snapshot_interval > 0 and epoch % snapshot_interval == 0):
            try:
                with timer.phase('snapshot'):
                    inputs = self._snapshot_inputs(task, redis_service)
                if inputs is not None and state() is None:
                    inputs = None
                if inputs is None:
                    # A later epoch has committed: this merger was delayed past
                    # a takeover whose publisher already wrote this snapshot.
                    # Its time and a(0) are gone from Redis, so writing it now
                    # would overwrite the sidecar with a stale one.
                    logger.info(f"Job {job_id} epoch {epoch}: state moved on, snapshot left "
                                f"to the epoch's publisher")
                    pending = None
                else:
                    sim_time, accelerations = inputs
                    pending = upload_snapshot_async(
                        job_id, epoch, merged, next_dt=next_dt,
                        simulation_time=float(sim_time) if sim_time not in (None, '') else None,
                        dt_used=task.time_step,
                        accelerations=accelerations,
                        generation=gen,
                        # re-checked in the upload thread, right before each write
                        is_current=lambda: _fence(redis_service, job_id).is_current(gen),
                        # an S3 outage drops queued interval snapshots, never the final one
                        droppable=not is_final,
                    )
                    # Land the final snapshot before the job is marked COMPLETED, so a
                    # reader of a completed job never misses its last frame
                    if is_final and pending is not None:
                        try:
                            pending.result(timeout=FINAL_SNAPSHOT_TIMEOUT_S)
                        except concurrent.futures.TimeoutError:
                            # the upload thread keeps going (never dropped). Only
                            # the wait for it before completing the job ends here
                            logger.warning(f"Final snapshot of job {job_id} (epoch {epoch}) not "
                                           f"uploaded after {FINAL_SNAPSHOT_TIMEOUT_S} s, completing "
                                           f"the job while its upload continues in the background")
            except Exception as e:
                logger.warning(f"Failed to upload snapshot for job {job_id} epoch {epoch}: {e}")
            if merged is not None or task_timing_diagnostics_enabled():
                self._record_diagnostics(task, epoch, state(), redis_service, timer)

        if epoch >= task.num_epochs:
            # The final epoch: cleanup, then finalize (PostgreSQL and S3), then
            # the end of publication, as separate steps
            with timer.phase('cleanup'):
                redis_service.cleanup_epoch_data(job_id, epoch, gen)
            final_state = state()
            if final_state is None:
                # the final epoch's state is gone (no later epoch can exist)
                logger.error(f"Job {job_id}: final state of epoch {epoch} is missing, "
                             f"leaving the epoch pending")
                return
            with timer.phase('finalize'):
                finalized = self._finalize_job(task, final_state, redis_service)
            if not finalized:
                return  # left pending. Retried by a takeover
            fault_point('after_publish', job_id, epoch, self.worker_name, redis_service)
            with timer.phase('finish'):
                redis_service.finish_publication(job_id, gen, epoch)
            return

        fault_point('before_publish', job_id, epoch, self.worker_name, redis_service)
        (template, n_tasks, package_size), target_stream, dt_for_next_epoch = self._next_epoch_tasks(
            task, next_dt, n_bodies)
        if renewer is not None:
            # the script ends the lease: no renewal may run after it
            renewer.stop()
        with timer.phase('publish'):
            _cleanup, result, published = redis_service.publish_epoch(
                job_id, gen, epoch, token or '', n_tasks, n_bodies, package_size, template,
                dt_for_next_epoch, str(dt_for_next_epoch), stream=target_stream)
        if not published:
            # The commit this publication follows is gone (a Redis failover
            # lost it before it reached a replica). The script changed
            # nothing: the epoch stays pending, so a takeover commits it again
            # (the caller releases the lease).
            logger.error(f"Job {job_id}: commit of epoch {epoch} is missing, "
                         f"leaving the epoch to a takeover")
            return
        if lease_state is not None and token:
            lease_state['released'] = True
        if result == JOB_PAUSED:
            logger.info(f"Job {job_id} paused at epoch {epoch}, not queuing next epoch")
        elif result < 0:
            logger.info(f"Job {job_id}: epoch {epoch + 1} not queued (code {result}: the job is "
                        f"not running, or a later publisher already advanced it)")
        elif result > 0:
            self._after_enqueue(task, target_stream, dt_for_next_epoch)
        fault_point('after_publish', job_id, epoch, self.worker_name, redis_service)

    @staticmethod
    def _snapshot_inputs(task: TaskMessage, redis_service):
        """(simulation time, a(0) blob or None) of the state committed for this
        epoch, for its snapshot sidecar. None if the state has moved on.

        The fence is read LAST: the commit of a later epoch changes the fence,
        the time and the a(0) payload in one script, so a fence still at this
        epoch after the other reads means they all belong to this epoch, and
        the upload is correct however late it lands."""
        sim_time = redis_service.get_job_meta(task.job_id).get('sim_time')
        blob = (redis_service.get_accelerations(task.job_id)
                if get_predictor_order(task.integration_method) >= 2 else None)
        if not _fence(redis_service, task.job_id).holds_state_of(task.generation, task.epoch_num):
            return None
        return sim_time, blob

    def _next_epoch_tasks(self, task: TaskMessage, next_dt: float, n_bodies: int):
        """((job-level template, number of tasks, package size), stream, step)
        of the next epoch for publish_epoch."""

        next_epoch = task.epoch_num + 1
        # Carried by the task, package_size and the GPU routing included (a
        # lost meta is refused by the enqueue script)
        params = EpochParams.from_task(task)
        # Preserve GPU routing across epochs (set by the orchestrator at creation)
        target_stream = GPU_TASKS_STREAM if params.gpu_enabled else TASKS_STREAM
        dt_for_next_epoch = next_dt if next_dt is not None else task.time_step
        tpl = epoch_task_template(task.job_id, task.generation, next_epoch, n_bodies,
                                  dt_for_next_epoch, params)
        return (tpl, params.total_packages, params.package_size), target_stream, dt_for_next_epoch

    def _after_enqueue(self, task: TaskMessage, target_stream: str, dt_for_next_epoch: float) -> None:
        """Bookkeeping after the next epoch was enqueued: stream trim and the
        PostgreSQL progress sync, on their cadences."""
        redis_service = self.messages.get_redis_service()
        next_epoch = task.epoch_num + 1
        # Bound the shared task stream. XACK removes a task from the consumer
        # group's pending list but does NOT delete it from the stream, so without
        # a periodic XTRIM the v2:tasks stream grows one entry per (epoch ×
        # package) forever and eventually exhausts Redis maxmemory. Prune on a
        # cadence. The trim keeps every entry not yet delivered or still pending
        # (RedisService.prune_task_stream).
        if next_epoch % TASK_STREAM_PRUNE_INTERVAL_EPOCHS == 0:
            redis_service.prune_task_stream(stream=target_stream)

        # Sync progress to PostgreSQL on snapshot boundaries so the UI/watchdog see live progress
        # (V2 stores hot state in Redis, PG is durable record updated at snapshot cadence)
        snapshot_interval = task.snapshot_interval or 0
        if (snapshot_interval > 0 and next_epoch % snapshot_interval == 0) or next_epoch == task.num_epochs:
            try:
                Job.update(
                    current_epoch=next_epoch,
                    updated_at=datetime.now(timezone.utc),
                    retry_count=0,  # progress resets the retry budget
                ).where(Job.job_id == task.job_id).execute()
            except Exception as e:
                logger.warning(f"Failed to sync epoch {next_epoch} to PG for job {task.job_id}: {e}")

        if task.adaptive_enabled:
            logger.debug(f"Queued {task.total_packages} tasks for epoch {next_epoch} "
                         f"(adaptive dt={dt_for_next_epoch:.4e})")
        else:
            logger.debug(f"Queued {task.total_packages} tasks for epoch {next_epoch}")

    def _finalize_job(self, task: TaskMessage, final_binary: Optional[bytes], redis_service) -> bool:
        """Finalize a completed job. Returns True once the job is COMPLETED
        (by this call or an earlier one), False if another worker is finalizing
        or finalization failed, so the epoch stays pending and is retried.
        final_binary is None on a finalize retry of a job Redis already has
        COMPLETED (only the PostgreSQL record is left, a job not COMPLETED by
        then belongs to a later generation and is refused before any upload)."""

        def record_in_postgres(results_key: str) -> bool:
            # Conditional, so a cancel that reached only PostgreSQL is kept. An
            # error leaves the epoch pending and a takeover retries (the Redis
            # status is then COMPLETED and we come straight back here).
            try:
                now = datetime.now(timezone.utc)
                completed_now = Job.update(
                    status=JobStatus.COMPLETED.value,
                    current_epoch=task.epoch_num,
                    results_file_key=results_key,
                    updated_at=now,
                    completed_at=now,
                ).where(
                    (Job.job_id == task.job_id)
                    & (Job.status.not_in((JobStatus.CANCELLED.value, JobStatus.FAILED.value)))
                    & (Job.completed_at.is_null())
                ).execute()
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to record completion of job {task.job_id} in PostgreSQL "
                             f"(will be retried): {e}")
                return False
            if completed_now:            # counted once, not on a retried finalize
                try:
                    n_bodies = (len(final_binary) // BODY_SIZE if final_binary is not None
                                else redis_service.get_body_count(task.job_id))
                    prometheus_metrics.record_job_completed(n_bodies, task.epoch_num)
                except Exception as e:  # noqa: BLE001 — metrics only
                    logger.debug(f"Could not record completion metrics: {e}")
            try:
                hosts = redis_service.get_compute_hosts(task.job_id)
                if hosts:
                    record_compute_hosts(task.job_id, hosts)
            except Exception as e:  # noqa: BLE001 — bookkeeping only
                logger.warning(f"Could not copy the compute hosts of job {task.job_id}: {e}")
            return True

        meta = redis_service.get_job_meta(task.job_id)
        status = meta.get('status')
        if status == JobStatusV2.COMPLETED.value:
            return record_in_postgres(meta.get('results_file_key', ''))
        if status is None:
            logger.error(f"Job {task.job_id} meta is gone from Redis at finalization, the final "
                         f"state of epoch {task.epoch_num} is dropped. Recover the job from its "
                         f"latest snapshot.")
            return True
        if status in (JobStatusV2.CANCELLED.value, JobStatusV2.FAILED.value):
            # a cancel (or failure) that arrived after the last commit wins
            logger.info(f"Job {task.job_id} is {status}, not finalizing")
            return True
        lock = redis_service.acquire_finalization_lock(task.job_id)
        if lock is None:
            return False

        try:
            # A finalizer from before a recovery must not write the shared
            # results object: check the fence before the upload, not only in
            # finalize_job_meta after it. This narrows the window to the
            # upload itself. The key is still shared across generations.
            meta = redis_service.get_job_meta(task.job_id)
            fence = FenceState.of(meta)
            if not (fence.is_current(task.generation) and fence.committed_is(task.epoch_num)):
                logger.info(f"Job {task.job_id}: finalizer of generation {task.generation}, epoch "
                            f"{task.epoch_num} is stale (now generation {meta.get('generation')}, "
                            f"committed epoch {meta.get('committed_epoch')}), not uploading")
                return True

            logger.info(f"Finalizing job {task.job_id}")

            # Upload final CSV (idempotent: same key and content on a retry)
            results_key = upload_final_results_csv(task.job_id, final_binary, task.epoch_num)

            # Status and checks in one script: a cancel or a recovery that
            # landed during the upload is not overwritten.
            result = redis_service.finalize_job_meta(task.job_id, task.generation, task.epoch_num, results_key)
            if result < 0:
                logger.info(f"Job {task.job_id} not finalized (code {result}: cancelled, failed "
                            f"or recovered meanwhile)")
                return True

            if not record_in_postgres(results_key):
                return False
            logger.info(f"Job {task.job_id} completed")
            return True

        except Exception as e:
            logger.error(f"Finalization of job {task.job_id} failed (will be retried): {e}")
            return False

        finally:
            redis_service.release_finalization_lock(task.job_id, lock)

    # Takeover of epochs whose merging worker died

    consumer_cleanup_interval_s: float = 600.0

    def _on_idle(self) -> None:
        now = time.monotonic()
        if now - self._last_takeover_scan < self.takeover_interval_s:
            return
        self._last_takeover_scan = now
        try:
            self._try_takeover()
        except Exception as e:  # noqa: BLE001 — never let a takeover scan kill the loop
            logger.warning(f"Takeover scan failed: {e}", exc_info=True)
        if now - self._last_consumer_cleanup >= self.consumer_cleanup_interval_s:
            self._last_consumer_cleanup = now
            try:
                rs = self.messages.get_redis_service()
                rs.cleanup_idle_consumers(stream=self.messages.stream)
                # Also trim the task stream here: the merge path trims only
                # every TASK_STREAM_PRUNE_INTERVAL_EPOCHS, which shorter jobs
                # never reach, so a run of short jobs would grow it without
                # bound (towards maxmemory with noeviction).
                rs.prune_task_stream(stream=self.messages.stream)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Consumer cleanup failed: {e}")

    def _try_takeover(self) -> int:
        """Finish pending epochs whose merge lease is not held. Returns the
        number of epochs this worker attempted."""
        rs = self.messages.get_redis_service()
        attempted = 0
        for job_id, gen, epoch in rs.get_pending_merges(older_than_s=self.takeover_grace_s, limit=50):
            if rs.merge_lease_owner(job_id, gen, epoch):
                continue  # a live merger is working on it
            meta = rs.get_job_meta(job_id)
            if not meta or 'status' not in meta:
                logger.error(f"Pending epoch {epoch} of job {job_id} has no job meta in Redis "
                             f"(lost or deleted), dropping it. Recover the job from its snapshot.")
                rs.drop_pending_merge(job_id, gen, epoch)
                continue
            fence = FenceState.of(meta)
            if not fence.is_current(gen) or fence.status in (JobStatusV2.CANCELLED.value,
                                                             JobStatusV2.FAILED.value):
                rs.drop_pending_merge(job_id, gen, epoch)
                continue
            logger.warning(f"Taking over merge of job {job_id} epoch {epoch} (generation {gen})")
            attempted += 1
            try:
                self._merge_and_advance(self._task_from_meta(job_id, gen, epoch, meta), rs)
            except Exception as e:  # noqa: BLE001
                # e.g. S3 down during finalize: move the entry to the back so
                # it cannot hide other dead epochs, and retry on a later scan
                logger.warning(f"Takeover of job {job_id} epoch {epoch} failed: {e}")
                rs.requeue_pending_merge(job_id, gen, epoch)
        return attempted

    @staticmethod
    def _task_from_meta(job_id: int, generation: int, epoch: int, meta: dict) -> TaskMessage:
        """Rebuild the job-level task fields a merge needs from the job meta."""
        params = EpochParams.from_meta(meta)
        time_step = float(meta.get('time_step', 0.0))
        # The step this epoch took: epoch_dt holds the step of the epoch last
        # enqueued, which is a later one once this epoch is committed and the
        # next enqueued. The commit records the committed epoch's own step.
        if meta.get('committed_epoch') == str(epoch) and meta.get('committed_dt'):
            epoch_dt = float(meta['committed_dt'])
        else:
            epoch_dt = float(meta.get('epoch_dt') or 0.0)
        return TaskMessage(
            message_id='takeover',
            job_id=job_id,
            bucket=meta.get('input_bucket', ''),
            file_key=meta.get('input_file_key', ''),
            epoch_num=epoch,
            package_num=0,
            package_id=None,
            package_size=params.package_size,
            total_packages=params.total_packages,
            num_epochs=params.num_epochs,
            time_step=epoch_dt if epoch_dt > 0 else time_step,
            snapshot_interval=params.snapshot_interval,
            algorithm=params.force_algorithm,
            barnes_hut_theta=params.barnes_hut_theta,
            softening_sq=params.softening_sq,
            integration_method=params.integration_method,
            gpu_enabled=params.gpu_enabled,
            adaptive_enabled=params.adaptive_enabled,
            adaptive_eta=params.adaptive_eta,
            adaptive_dt_min=params.adaptive_dt_min,
            adaptive_dt_max=params.adaptive_dt_max,
            generation=generation,
        )


class _HeldLease:
    """A merge lease held by one merge and renewed by _MergeLeaseKeeper.

    `lost` becomes True once a renewal is refused (the lease was taken over).
    stop() ends the renewal: no renewal runs after it returns."""

    def __init__(self, keeper, redis_service, args, interval_s: float):
        self._keeper = keeper
        self._rs = redis_service
        self._args = args
        self.interval_s = interval_s
        self.next_due = time.monotonic() + interval_s
        self.lost = False
        self._stopped = False
        self._lock = threading.Lock()

    def renew_once(self) -> None:
        with self._lock:
            if self._stopped:
                return
            if FROZEN.is_set():
                # a frozen process renews nothing
                self.next_due = time.monotonic() + self.interval_s
                return
            try:
                ok = self._rs.renew_merge_lease(*self._args)
            except Exception as e:  # noqa: BLE001 — retried soon (Redis failing over)
                logger.warning(f"Merge lease renewal failed: {e}")
                self.next_due = time.monotonic() + min(self.interval_s, RenewLoop.RETRY_AFTER_FAILURE_S)
                return
            if not ok:
                self.lost = True
                self._stopped = True
                self._keeper.forget(self)
                return
            self.next_due = time.monotonic() + self.interval_s

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
        self._keeper.forget(self)


class _MergeLeaseKeeper:
    """Renews the merge leases this process's merges hold, from one daemon
    thread started on first use. A merge takes milliseconds against a lease
    of tens of seconds, renewed at a third of its length, so almost no merge
    is ever renewed, and a renewal thread per merge would cost more than the
    merge's own bookkeeping. hold() only registers the lease."""

    def __init__(self):
        self._cv = threading.Condition()
        self._held = set()
        self._thread = None

    def hold(self, redis_service, job_id: int, generation: int, epoch: int, token: str,
             lease_ms: int) -> _HeldLease:
        lease = _HeldLease(self, redis_service, (job_id, generation, epoch, token, lease_ms),
                           lease_ms / 3000.0)
        with self._cv:
            self._held.add(lease)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name='merge-lease', daemon=True)
                self._thread.start()
            self._cv.notify()
        return lease

    def forget(self, lease: _HeldLease) -> None:
        with self._cv:
            self._held.discard(lease)

    def _run(self) -> None:
        while True:
            with self._cv:
                now = time.monotonic()
                due = [h for h in self._held if h.next_due <= now]
                if not due:
                    nxt = min((h.next_due for h in self._held), default=None)
                    self._cv.wait(timeout=None if nxt is None else max(0.0, nxt - now))
                    continue
            for lease in due:
                lease.renew_once()


_MERGE_LEASES = _MergeLeaseKeeper()
