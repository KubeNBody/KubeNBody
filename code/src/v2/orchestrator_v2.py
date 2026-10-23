# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
This module handles:
1. Creating new V2 jobs from bodies the API has already parsed and validated
2. Converting them to binary format and storing them in Redis
3. Queueing epoch tasks (creation, resume, recovery)
4. Recovering a job from its latest S3 snapshot (or its input, before the first)

Unlike v1, this is not a separate long-running service.
It's called by the API to initialize jobs.
"""

import math
from typing import Any, Callable, Dict, List, Optional

from constants import DEFAULT_THETA, SOFTENING_SQ
from file_service import download_file
from logging_config import setup_logging
from utils.file_parsing import parse_bodies
from v2.redis_service import GPU_TASKS_STREAM, TASKS_STREAM
from workers.renew_loop import RenewLoop

from .binary_format import ACCEL_SIZE, BODY_SIZE, bodies_to_binary
from .epoch_tasks import EpochParams, build_epoch_tasks
from .file_service_v2 import (
    download_snapshot,
    download_snapshot_aux,
    list_snapshots,
    validate_binary_data,
)
from .partitioning import (
    DEFAULT_PARTITION_SEED,
    DEFAULT_PARTITION_STRATEGY,
    apply_partition,
)
from .redis_service import JOB_MISSING, RECOVERY_LOCK_LOST, RECOVERY_LOCK_RENEW_S, get_redis_service
from .schemas_v2 import JobMetaV2, JobStatusV2


logger = setup_logging('orchestrator-v2')

class RecoveryConflict(Exception):
    """A recovery refused or stopped by a concurrent change: another recovery
    of the job holds its lock, or the job is (or was meanwhile) cancelled."""


def _recovery_lock_renewer(redis, job_id: int, token: str):
    """Started thread renewing a recovery's lock until stopped: the lock's
    short TTL then only runs out for a recovery whose process died."""

    class _Renewer(RenewLoop):
        name = f'recover-lock-{job_id}'
        what = f'Renewal of the recovery lock of job {job_id}'
        log = logger

        def renew(self) -> bool:
            return redis.renew_recovery_lock(job_id, token)

        def on_lost(self) -> None:
            logger.warning(f"Job {job_id}: the recovery lock was lost, the recovery is refused "
                           f"at its generation bump if it has not passed it yet")

    renewer = _Renewer(RECOVERY_LOCK_RENEW_S)
    renewer.start()
    return renewer


def _load_input_bodies(bucket: str, key: str) -> List[Dict]:
    """The bodies of a job's input file, parsed as job creation parses them
    (utils.input_files), in file order."""
    return parse_bodies(download_file(bucket, key))


class OrchestratorV2:
    """V2 orchestrator for job initialization and management."""

    def __init__(self):
        self.redis = get_redis_service()

    def create_job_with_bodies(
        self,
        job_id: int,
        bodies: List[Dict],
        num_epochs: int,
        time_step: float,
        package_size: int,
        snapshot_interval: int,
        input_bucket: str,
        input_file_key: str,
        force_algorithm: str = "direct",
        barnes_hut_theta: Optional[float] = None,
        softening_sq: float = SOFTENING_SQ,
        integration_method: str = "symplectic_euler",
        adaptive_enabled: bool = False,
        adaptive_eta: float = 0.02,
        adaptive_dt_min: float = 0.0,
        adaptive_dt_max: float = 0.0,
        gpu_enabled: bool = False,
        partition_strategy: str = DEFAULT_PARTITION_STRATEGY,
        partition_seed: int = DEFAULT_PARTITION_SEED,
    ) -> int:
        """Create a V2 job from bodies the API has already parsed and validated
        (so the input file is not downloaded and parsed again), and queue its
        first epoch. snapshot_interval 0 means final snapshot only. The input
        bucket and key are stored as metadata. Returns job_id."""
        n_bodies = len(bodies)

        if n_bodies == 0:
            raise ValueError("Input file contains no bodies")

        if barnes_hut_theta is None:
            barnes_hut_theta = DEFAULT_THETA

        logger.debug(f"Initializing job {job_id} with {n_bodies} bodies")
        logger.info(f"Job {job_id} using force algorithm: {force_algorithm}" +
                   (f" (theta={barnes_hut_theta})" if force_algorithm == "barnes_hut" else "") +
                   f", integration: {integration_method}")

        total_packages = math.ceil(n_bodies / package_size)

        logger.debug(f"Splitting into {total_packages} packages of size {package_size}")

        # Permute the body order once, so contiguous package slices take the
        # requested shape (see v2/partitioning.py). Recovery restores an
        # already-permuted snapshot and must not permute again. Only its
        # restart from the input (no snapshot yet) applies the partition.
        bodies = apply_partition(bodies, partition_strategy, partition_seed, package_size)

        binary_data = bodies_to_binary(bodies)
        self.redis.store_bodies(job_id, binary_data)

        logger.debug(f"Stored {len(binary_data)} bytes of body data in Redis")

        params = EpochParams(
            package_size=package_size,
            total_packages=total_packages,
            num_epochs=num_epochs,
            snapshot_interval=snapshot_interval,
            force_algorithm=force_algorithm,
            barnes_hut_theta=barnes_hut_theta,
            softening_sq=softening_sq,
            integration_method=integration_method,
            adaptive_enabled=adaptive_enabled,
            adaptive_eta=adaptive_eta,
            adaptive_dt_min=adaptive_dt_min if adaptive_dt_min > 0 else 1e-10,
            adaptive_dt_max=adaptive_dt_max if adaptive_dt_max > 0 else time_step,
            gpu_enabled=gpu_enabled,
        )

        meta = JobMetaV2(
            job_id=job_id,
            num_epochs=num_epochs,
            current_epoch=0,
            time_step=time_step,
            total_packages=total_packages,
            package_size=package_size,
            snapshot_interval=snapshot_interval,
            status=JobStatusV2.IN_PROGRESS.value,
            input_bucket=input_bucket,
            input_file_key=input_file_key,
            force_algorithm=force_algorithm,
            barnes_hut_theta=barnes_hut_theta,
            softening_sq=softening_sq,
            integration_method=integration_method,
            adaptive_enabled=adaptive_enabled,
            adaptive_eta=adaptive_eta,
            adaptive_dt_min=params.adaptive_dt_min,
            adaptive_dt_max=params.adaptive_dt_max,
            gpu_enabled=gpu_enabled,
            partition_strategy=partition_strategy,
            partition_seed=partition_seed,
            generation=0,
            committed_epoch=0,
            epoch_dt=time_step,
        )
        self.redis.set_job_meta(job_id, meta.to_redis_dict())

        self._queue_epoch_tasks(job_id, 0, 1, n_bodies, time_step, params)

        logger.info(f"Job {job_id} created: {n_bodies} bodies, {total_packages} packages, "
                    f"{num_epochs} epochs")

        return job_id

    def _queue_epoch_tasks(self, job_id: int, generation: int, epoch: int, n_bodies: int,
                           time_step: float, params: EpochParams) -> int:
        """Queue all tasks of an epoch (step time_step), exactly once per
        generation. Returns the number of tasks queued (0 if this epoch was
        already queued)."""
        tasks = build_epoch_tasks(job_id, generation, epoch, n_bodies, time_step, params)
        stream = GPU_TASKS_STREAM if params.gpu_enabled else TASKS_STREAM
        queued = self.redis.enqueue_epoch_tasks(
            job_id, generation, epoch, tasks, time_step, stream=stream, allow_paused=True,
        )
        logger.debug(f"Queued {queued} tasks for job {job_id} epoch {epoch} "
                     f"(generation {generation}, gpu={params.gpu_enabled})")
        return queued

    def get_job_status(self, job_id: int) -> Optional[dict]:
        """The job's status from Redis, None if Redis does not have the job."""
        meta = self.redis.get_job_meta(job_id)
        if not meta:
            return None

        results_file_key = None
        if meta.get('status') == JobStatusV2.COMPLETED.value:
            # recorded by the finalization script. The default key otherwise
            results_file_key = meta.get('results_file_key') or f"v2/job_id/{job_id}/final_results.csv"

        return {
            'job_id': int(meta['job_id']),
            'status': meta['status'],
            'current_epoch': int(meta['current_epoch']),
            'num_epochs': int(meta['num_epochs']),
            'total_packages': int(meta['total_packages']),
            'snapshot_interval': int(meta['snapshot_interval']),
            'results_file_key': results_file_key,
            'generation': int(meta.get('generation', 0) or 0),
            # epochs whose a(0) was returned / a fallback
            'accel_sources': {k[len('accel_src_'):]: int(v) for k, v in meta.items()
                              if k.startswith('accel_src_')} or None,
        }

    def cancel_job(self, job_id: int) -> bool:
        """Cancel a job that has not reached a final state. False if it is already
        CANCELLED, FAILED or COMPLETED in Redis (left as is). True if cancelled, or
        if Redis has no meta for it (nothing to stop there, the meta is not
        recreated)."""
        result = self.redis.cancel_job(job_id)
        if result == JOB_MISSING:
            logger.info(f"Job {job_id} has no Redis meta, nothing to cancel there")
            return True
        if result < 0:
            # Already CANCELLED counts as success: a cancel whose reply was lost
            # in a failover is retried and then finds its own write.
            if self.redis.get_fence_state(job_id).get('status') == JobStatusV2.CANCELLED.value:
                logger.info(f"Job {job_id} is already cancelled")
                return True
            logger.info(f"Job {job_id} not cancelled: already in a final state")
            return False
        logger.info(f"Job {job_id} cancelled")
        return True

    def pause_job(self, job_id: int) -> bool:
        """Pause a running V2 job. Workers will stop after current epoch.

        Only a PENDING / IN_PROGRESS (or already PAUSED) job is paused. Its
        Redis keys lose their TTL until it is resumed, however long that takes.
        """
        result = self.redis.pause_job(job_id)
        if result < 0:
            logger.info(f"Job {job_id} not paused (code {result}: finished or not in Redis)")
            return False
        logger.info(f"Job {job_id} paused")
        return True

    def resume_job(self, job_id: int) -> bool:
        """Resume a paused V2 job by requeuing epoch tasks.

        Returns False, leaving the job PAUSED, if it is not paused, has no
        epoch left, or its Redis meta or state is gone (recover it instead).
        """
        result = self.redis.resume_job(job_id)
        if result < 0:
            logger.warning(f"Job {job_id} cannot be resumed (code {result}: not paused, finished, "
                           f"or its Redis state is gone)")
            return False

        # Resume after the last committed epoch, read AFTER the status change:
        # a merge that commits before it sees PAUSED at its enqueue and leaves
        # the next epoch to us (the commit precedes the enqueue, so we see it).
        # One that enqueues after it sees IN_PROGRESS and queues the epoch
        # itself, and the enqueue guard makes this call a no-op. Reading
        # committed_epoch before the status change could miss the first case
        # and stall the job.
        meta = self.redis.get_job_meta(job_id)
        committed = meta.get('committed_epoch')
        last_done = int(committed) if committed is not None else int(meta['current_epoch'])
        next_epoch = last_done + 1
        if next_epoch > int(meta['num_epochs']):
            # the epoch in flight at the pause was the last and committed
            # after the check above: its merger finalizes the job
            return True

        n_bodies = self.redis.get_body_count(job_id)   # (not the state itself: 64 MB at 1e6)
        if n_bodies == 0:
            raise ValueError(f"Job {job_id} has no body state in Redis, recover it from a snapshot")

        # the step the last commit chose for the next epoch (adaptive dt).
        # epoch_dt is that of the epoch last enqueued, which a pause before
        # the enqueue leaves one epoch behind
        time_step = (float(meta.get('next_dt') or 0.0) or float(meta.get('epoch_dt') or 0.0)
                     or float(meta['time_step']))
        self._queue_epoch_tasks(job_id, int(meta.get('generation', 0)), next_epoch, n_bodies,
                                time_step, EpochParams.from_meta(meta))
        logger.info(f"Job {job_id} resumed, queued epoch {next_epoch}")
        return True

    def recover_job(self, job_id: int, pg_job: Any = None,
                    on_restored: Optional[Callable[[int], None]] = None,
                    if_generation: Optional[int] = None) -> Optional[int]:
        """Recover a job from its latest S3 snapshot, or from its input file
        when it has none yet (snapshot_interval=0 writes only the final one).
        Returns the epoch restored to (0: restarted from the input), or None if
        neither a usable snapshot nor the input (or the metadata) is found.

        Used when Redis data is lost, or the job failed or stalled. pg_job, the
        PostgreSQL row, is the fallback for the metadata. Recovering a PAUSED job
        resumes it. A CANCELLED job is never recovered.

        The whole recovery holds the job's recovery lock, and everything it
        downloads is read before it writes anything. Its writes start with the
        generation bump and status flip (one script, refused for a cancelled
        job), and each one after is fenced on the new generation and on the
        job not having been cancelled since: a recovery that lost its lock, or
        one a cancel overtook, stops without touching the state.

        on_restored: called with the recovered epoch once the Redis state is
        restored, before any task or pending merge is queued (the API records the
        recovery in PostgreSQL there): no worker can run, let alone finalize, the
        recovered job before its row says it is running again. It may raise
        RecoveryConflict to stop the recovery (nothing is queued then). Any other
        error stops it too and propagates, leaving the job to be recovered again.

        if_generation: recover only a job still at this generation (a worker
        recovering the epoch it found lost: another worker that found the same
        loss must not recover the job a second time).

        Raises RecoveryConflict when another recovery of the job is running or the
        job was cancelled (before or during this recovery), and the object store's
        botocore errors (nothing was written then)."""
        token = self.redis.acquire_recovery_lock(job_id)
        if token is None:
            raise RecoveryConflict(f"Job {job_id} is already being recovered, retry once that "
                                   f"recovery has finished")
        renewer = _recovery_lock_renewer(self.redis, job_id, token)
        try:
            if if_generation is not None:
                current = int((self.redis.get_job_meta(job_id) or {}).get('generation') or 0)
                if current != if_generation:
                    raise RecoveryConflict(f"Job {job_id} is at generation {current}, not {if_generation}: "
                                           f"already recovered")
            return self._recover_locked(job_id, pg_job, token, on_restored)
        finally:
            renewer.stop()
            try:
                self.redis.release_recovery_lock(job_id, token)
            except Exception as e:  # noqa: BLE001 — must not replace the recovery's outcome
                logger.warning(f"Job {job_id}: could not release the recovery lock "
                               f"(it expires on its own): {e}")

    def _recover_locked(self, job_id: int, pg_job: Any, lock_token: str,
                        on_restored: Optional[Callable[[int], None]] = None) -> Optional[int]:

        # Find latest snapshot (an S3 error other than a missing bucket raises:
        # it must not pass for a job without snapshots)
        epochs = list_snapshots(job_id)
        binary_data = None
        latest_epoch = 0
        if epochs:
            latest_epoch = epochs[-1]
            logger.info(f"Recovering job {job_id} from snapshot at epoch {latest_epoch}")
            binary_data = download_snapshot(job_id, latest_epoch)
            if not validate_binary_data(binary_data):
                logger.error(f"Corrupted snapshot at epoch {latest_epoch} for job {job_id}")
                # Try previous snapshot if available
                if len(epochs) > 1:
                    previous_epoch = epochs[-2]
                    logger.info(f"Trying previous snapshot at epoch {previous_epoch}")
                    binary_data = download_snapshot(job_id, previous_epoch)
                    if not validate_binary_data(binary_data):
                        logger.error(f"Previous snapshot also corrupted for job {job_id}")
                        return None
                    latest_epoch = previous_epoch
                else:
                    return None

        # Get job metadata - try Redis first, fall back to PostgreSQL
        meta = self.redis.get_job_meta(job_id)
        rebuilt = False

        if not meta and pg_job:
            # Reconstruct metadata from PostgreSQL
            logger.info(f"Using PostgreSQL metadata for job {job_id} recovery")
            meta = {
                'job_id': str(job_id),
                'num_epochs': str(pg_job.num_epochs),
                'current_epoch': str(latest_epoch),
                'time_step': str(pg_job.time_step),
                'total_packages': str(pg_job.num_packages),
                'package_size': str(pg_job.package_size),
                'snapshot_interval': str(pg_job.snapshot_interval),
                'status': JobStatusV2.IN_PROGRESS.value,
                'input_bucket': str(pg_job.input_bucket or 'inputfiles'),
                'input_file_key': str(pg_job.input_file_key),
                'force_algorithm': str(pg_job.force_algorithm or 'direct'),
                'barnes_hut_theta': str(pg_job.barnes_hut_theta or 0.5),
                'softening_sq': str(pg_job.softening_sq if pg_job.softening_sq is not None else SOFTENING_SQ),
                'integration_method': str(pg_job.integration_method or 'symplectic_euler'),
                'adaptive_enabled': str(pg_job.adaptive_enabled),
                'adaptive_eta': str(pg_job.adaptive_eta or 0.02),
                'adaptive_dt_min': str(pg_job.adaptive_dt_min or 1e-10),
                'adaptive_dt_max': str(pg_job.adaptive_dt_max or pg_job.time_step),
                'gpu_enabled': str(bool(pg_job.gpu_enabled)),
                'partition_strategy': str(pg_job.partition_strategy or 'linear'),
                'partition_seed': str(pg_job.partition_seed or 0),
                # The generation the lost meta had is unknown: a number no
                # earlier incarnation used, so its leftover tasks and pending
                # merges (e.g. 'job:1:epoch') cannot pass for the new one's.
                'generation': str(self.redis.fresh_generation_seed()),
            }
            rebuilt = True

        if not meta:
            logger.error(f"Job {job_id} metadata not found in Redis or PostgreSQL, cannot recover")
            return None

        params = EpochParams.from_meta(meta)
        snapshot_accel = None
        snapshot_time = None
        dt_used = None
        if binary_data is not None:
            # The snapshot's sidecar: the step its next epoch used and the
            # shipped a(0) of its state (absent for snapshots written before
            # sidecars), and the clock its state had reached (unknown for
            # snapshots older than the time sidecar).
            aux = download_snapshot_aux(job_id, latest_epoch)
            snapshot_accel, snapshot_time = aux.accelerations, aux.simulation_time
            n_bodies = len(binary_data) // BODY_SIZE
            if snapshot_accel is not None and len(snapshot_accel) != n_bodies * ACCEL_SIZE:
                logger.warning(f"Ignoring a(0) sidecar of snapshot {latest_epoch} (size mismatch)")
                snapshot_accel = None
            # The step the snapshot's next epoch used (an adaptive job
            # restarted at the initial step would not match the uninterrupted
            # run). The meta's next_dt belongs to a later commit.
            time_step = aux.next_dt if aux.next_dt else float(meta['time_step'])
            if latest_epoch >= params.num_epochs:
                dt_used = aux.dt_used
        else:
            # No snapshot yet: start over from the input, exactly as creation
            # did (same parsing, same partition), at the initial step. The
            # workers compute the a(0) of epoch 1, as they do on creation.
            bucket, key = meta.get('input_bucket'), meta.get('input_file_key')
            if not key or key == 'None':
                logger.error(f"Job {job_id} has no snapshot and no input file recorded, cannot recover")
                return None
            logger.info(f"Job {job_id} has no snapshot: recovering from its input {bucket}/{key}")
            bodies = apply_partition(
                _load_input_bodies(bucket, key),
                meta.get('partition_strategy') or DEFAULT_PARTITION_STRATEGY,
                int(meta.get('partition_seed') or DEFAULT_PARTITION_SEED),
                params.package_size,
            )
            binary_data = bodies_to_binary(bodies)
            n_bodies = len(bodies)
            time_step = float(meta['time_step'])

        # Everything is read: from here on, write.
        if rebuilt:
            self.redis.set_job_meta(job_id, meta)
        elif 'generation' not in meta:
            # a meta without the fence fields (a job from before them, or a
            # partial rebuild): same reasoning as for the rebuilt one
            self.redis.update_job_meta(job_id, 'generation', self.redis.fresh_generation_seed())

        # Start a new generation FIRST: from here on, every task, registration,
        # commit, a(0) shipment or enqueue issued under the old generation is
        # rejected, so a worker or merger still running from before the crash
        # cannot write into the restored state. Epoch keys are scoped by
        # generation, so the new one starts every epoch afresh. The old
        # generations' keys are deleted below. The generation is a fencing
        # token, not a count of recoveries (PostgreSQL's Job.recovery_count).
        generation = self.redis.begin_recovery(job_id, lock_token)
        if generation == RECOVERY_LOCK_LOST:
            raise RecoveryConflict(f"Job {job_id}: this recovery outlived its lock and another may "
                                   f"have started, nothing was changed")
        if generation == JOB_MISSING:
            raise RecoveryConflict(f"Job {job_id}: its Redis metadata disappeared during the recovery")
        if generation < 0:
            raise RecoveryConflict(f"Job {job_id} is cancelled, a cancelled job is not recovered")
        if rebuilt:
            logger.info(f"Job {job_id} meta rebuilt from PostgreSQL, new generation {generation}")

        # Restore body state and the fencing fields. Shipped accelerations in
        # Redis belong to a different (later) state: replace them with the
        # snapshot's own a(0), which makes the restart reproduce the
        # uninterrupted run bitwise also with returned accelerations. Without
        # it workers recompute a(0). The step that reached a final snapshot
        # goes to committed_dt, which the takeover's task and the rewritten
        # sidecar use (a rebuilt meta has none). The a(0)-source counters
        # (accel_src_*, get_job_status) are kept: the epochs re-run are counted
        # twice, but what they record is which sources a run used at all, and
        # a reset would hide a fallback from before the recovery.
        restored = self.redis.restore_recovered_state(
            job_id, generation, binary_data, snapshot_accel, latest_epoch, time_step,
            snapshot_time, committed_dt=dt_used or None)
        if restored < 0:
            raise self._overtaken(job_id, generation, restored)

        # Drop the earlier generations' epoch keys (counters and staged state
        # of whatever epoch they had in flight).
        deleted = self.redis.delete_other_generation_keys(job_id, generation)
        if deleted < 0:
            raise self._overtaken(job_id, generation, deleted)

        # Record the recovery (PostgreSQL) before queueing anything of the new
        # generation. Recorded after, a worker could finalize the job first,
        # and the late record would then set the finished job IN_PROGRESS for
        # good.
        if on_restored is not None:
            on_restored(latest_epoch)

        if latest_epoch < params.num_epochs:
            # The params carry the adaptive and GPU settings, or a recovered
            # adaptive job would continue at a fixed step and a GPU job on CPU.
            queued = self._queue_epoch_tasks(job_id, generation, latest_epoch + 1, n_bodies, time_step, params)
            if queued < 0:
                raise self._overtaken(job_id, generation, queued)
        else:
            # The final epoch is always snapshotted, so a job that reached the
            # end recovers to num_epochs with no epoch left to run: mark that
            # epoch pending for the new generation, so a worker's takeover
            # republishes it and finalizes the job again.
            self.redis.add_pending_merge(job_id, generation, latest_epoch)

        logger.info(f"Job {job_id} recovered from " +
                    (f"epoch {latest_epoch}" if epochs else "its input (no snapshot yet)"))
        return latest_epoch

    def _overtaken(self, job_id: int, generation: int, code: int) -> 'RecoveryConflict':
        """The error of a recovery whose fenced write was refused (code)."""
        status = self.redis.get_fence_state(job_id).get('status')
        logger.warning(f"Recovery of job {job_id} (generation {generation}) stopped: "
                       f"code {code}, status {status}")
        if status == JobStatusV2.CANCELLED.value:
            return RecoveryConflict(f"Job {job_id} was cancelled during its recovery")
        return RecoveryConflict(f"Job {job_id} changed during its recovery (status {status}), "
                                f"recover it again if needed")

    def cleanup_completed_job(self, job_id: int):
        """Clean up Redis data for a completed job.

        Call this after results have been persisted to S3.
        """
        self.redis.cleanup_job(job_id)
        logger.info(f"Cleaned up Redis data for job {job_id}")


# Global instance
_orchestrator: Optional[OrchestratorV2] = None


def get_orchestrator() -> OrchestratorV2:
    """Get or create the global orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = OrchestratorV2()
    return _orchestrator
