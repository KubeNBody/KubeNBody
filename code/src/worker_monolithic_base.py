# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Shared Base Module for Monolithic N-Body Workers
=================================================
Contains common functionality used by both CPU and GPU monolithic workers:
- Worker lease system (acquire, renew, release)
- Graceful shutdown handling
- Exception classes
- Checkpoint/resume logic
- Snapshot management
- CSV loading utilities
"""

import csv
import io
import json
import os
import queue
import signal
import socket
import threading
from datetime import timedelta
from typing import Dict, List, Tuple

import numpy as np

import prometheus_metrics
from constants import (
    LEASE_DURATION_SECONDS,
    LEASE_RENEWAL_INTERVAL_SECONDS,
    LEASE_RETRY_AFTER_FAILURE_S,
)
from file_service import (
    EPOCH_RESULTS_BUCKET_NAME,
    combine_epoch_snapshots,
    download_file,
    get_s3,
    input_csv_reader,
    upload_file,
)
from logging_config import setup_logging
from models import InputFile, Job, utcnow
from models import db as models_db
from schemas import JobStatus, MonolithicJobMessage
from transient_errors import is_retryable_s3_error, is_transient_error


logger = setup_logging('worker-monolithic-base')


# Exception Classes

class LeaseLostException(Exception):
    """Raised when worker loses lease to another worker during processing."""
    pass


class ShutdownException(Exception):
    """Raised when graceful shutdown requested (SIGTERM/SIGINT)."""
    pass


class JobCancelledException(Exception):
    """Raised when job is cancelled by user during processing."""
    pass


class JobPausedException(Exception):
    """Raised when job is paused by user during processing."""
    pass


# Graceful Shutdown State

class ShutdownHandler:
    """Manages graceful shutdown state and signal handling."""

    def __init__(self, worker_id: str, logger_instance, exit_when_idle: bool = False):
        # exit_when_idle: on a signal with no job in progress, raise
        # KeyboardInterrupt so a blocking consume loop stops at once instead of
        # waiting for the kubelet's SIGKILL at the end of the grace period.
        self.exit_when_idle = exit_when_idle
        self.shutdown_requested = False
        self.shutdown_lock = threading.Lock()
        self.current_job_id = None
        self.worker_id = worker_id
        self.logger = logger_instance

        # Register signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        self.logger.info("Graceful shutdown handlers registered")

    def _signal_handler(self, signum, frame):
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        with self.shutdown_lock:
            if self.shutdown_requested:
                return  # Ignore duplicate signals
            self.shutdown_requested = True

        signal_name = 'SIGTERM' if signum == signal.SIGTERM else 'SIGINT'
        if self.exit_when_idle and self.current_job_id is None:
            self.logger.warning(f"{signal_name} received while idle - exiting")
            raise KeyboardInterrupt
        self.logger.warning("=" * 80)
        self.logger.warning(f"{signal_name} RECEIVED - GRACEFUL SHUTDOWN")
        self.logger.warning(f"Current job: {self.current_job_id}")
        # the epoch loop checks the flag before every epoch
        self.logger.warning("Will stop before the next epoch and hand the job back")
        self.logger.warning("Note: No snapshot created - the job resumes from its last periodic snapshot "
                            "(snapshot_interval), epochs since then are recomputed")
        self.logger.warning("=" * 80)

    def check_shutdown(self) -> bool:
        """Check if shutdown was requested."""
        return self.shutdown_requested


# Worker Lease System

def acquire_job_lease(job_id: int, worker_id: str, handed_back=None) -> bool:
    """Atomically take the job's exclusive lease for worker_id (an UPDATE with
    WHERE conditions: one holder at a time). True if acquired, False if another
    worker holds it.

    handed_back: (holder, at) from the message of a job its holder handed back
    (rabbitmq.handback_marker). That holder's lease is then free too, unless it
    was renewed after `at`: the holder stopped working on the job when it handed
    it back, but may have expired its lease (database down) - waiting for the
    expiry left the job idle for LEASE_DURATION_SECONDS plus the watchdog's
    grace. A lease renewed since is that worker running the job again (a
    watchdog republish it took): not taken. holder and at come from the same
    worker's clock as its renewals.

    An unreachable database raises (is_transient_error) instead of answering
    False: the caller would drop the message as a duplicate, and a queued job
    has no owner for the watchdog to notice, so the job would be lost."""
    try:
        expiry_threshold = utcnow() - timedelta(seconds=LEASE_DURATION_SECONDS)

        free = (Job.worker_id.is_null()) | (Job.leased_at < expiry_threshold)
        if handed_back is not None:
            holder, at = handed_back
            free = free | ((Job.worker_id == holder) & (Job.leased_at <= at))
        result = Job.update(
            worker_id=worker_id,
            leased_at=utcnow(),
            updated_at=utcnow(),
            handed_back_at=None,        # taken: a later lapse is this worker's
        ).where((Job.job_id == job_id) & free).execute()

        if result > 0:
            logger.debug(f"[JOB {job_id}] Lease acquired by {worker_id}")

        return result > 0

    except Exception as e:
        if is_transient_error(e):
            raise
        logger.error(f"Failed to acquire lease for job {job_id}: {e}")
        return False


def renew_job_lease(job_id: int, worker_id: str) -> bool:
    """Renew the lease of a job we hold, periodically during processing. False
    if we lost it."""
    try:
        result = Job.update(
            leased_at=utcnow()
        ).where(
            (Job.job_id == job_id) &
            (Job.worker_id == worker_id)
        ).execute()

        if result > 0:
            logger.debug(f"[JOB {job_id}] Lease renewed")
            return True
        else:
            logger.warning(f"[JOB {job_id}] Lost lease (another worker took over)")
            return False

    except Exception as e:
        logger.error(f"Failed to renew lease for job {job_id}: {e}")
        return False


def release_job_lease(job_id: int, worker_id: str):
    """Release our lease when done processing (success or failure)."""
    try:
        Job.update(
            worker_id=None,
            leased_at=None
        ).where(
            (Job.job_id == job_id) &
            (Job.worker_id == worker_id)
        ).execute()

        logger.debug(f"[JOB {job_id}] Lease released")

    except Exception as e:
        logger.error(f"Failed to release lease for job {job_id}: {e}")


# A lease handed back with a delayed republish is dated this far past its
# expiry: claimable at once by a worker whose clock is up to this much ahead,
# and dead to the watchdog (LEASE_GRACE_SECONDS, 30 s) only this much sooner,
# i.e. after 25 s, past the 15 s retry delay. Should the watchdog still
# republish the job first (a busy queue), the republish is not counted
# (handed_back_at).
DELAYED_HANDBACK_MARGIN_S = 5


def expire_job_lease(job_id: int, worker_id: str, delayed: bool = False) -> bool:
    """Hand a job back without leaving it ownerless: keep worker_id but date
    the lease past its expiry, so the next consumer of a republished message
    can take it at once, and, should that publish fail, the watchdog sees an
    expired lease and republishes. (Releasing first, then failing to publish,
    would leave a running job with neither an owner nor a message.)

    delayed: the republish waits in a retry queue (an outage being waited
    out). The lease is then dated just past its expiry, so the watchdog does
    not also republish the job, counting a retry, while its message waits.
    It still recovers the job within about a minute should the publish fail.

    handed_back_at records the hand-back: should the watchdog republish the
    job before the hand-back's message is consumed (a busy queue), that
    republish is not counted as a retry."""
    try:
        # two lease durations back: claimable at once, and past the watchdog's
        # grace period at once, should the republish fail
        back = (LEASE_DURATION_SECONDS + DELAYED_HANDBACK_MARGIN_S if delayed
                else 2 * LEASE_DURATION_SECONDS)
        expired = utcnow() - timedelta(seconds=back)
        n = Job.update(leased_at=expired, handed_back_at=utcnow()).where(
            (Job.job_id == job_id) & (Job.worker_id == worker_id)).execute()
        return n > 0
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to expire lease for job {job_id}: {e}")
        return False


class LeaseRenewer:
    """Renews a job's lease from a background thread for as long as the job is
    owned, not only between epochs: input loading, checkpoint restore, the
    final upload and finalization renew it too.

    The message is acknowledged when the job is claimed, so the lease is the
    only record of a live owner: a worker that dies stops renewing, its lease
    expires after LEASE_DURATION_SECONDS, and the watchdog republishes the job.
    (Holding the delivery unacknowledged for the whole job instead made the
    broker's consumer_timeout, 30 min by default, cut off every longer job.)

    `lost` becomes True once a renewal finds the lease taken by another worker.
    The epoch loop then stops. A failed renewal (database down) does not count
    as lost and is retried every LEASE_RETRY_AFTER_FAILURE_S until one
    succeeds: waiting a whole interval, the watchdog could find the lease
    lapsed before the renewal that follows the outage.
    """

    def __init__(self, job_id: int, worker_id: str, interval: float = LEASE_RENEWAL_INTERVAL_SECONDS,
                 renew=None, db=None, retry_after_failure: float = LEASE_RETRY_AFTER_FAILURE_S):
        self.job_id, self.worker_id, self.interval = job_id, worker_id, interval
        self.retry_after_failure = min(retry_after_failure, interval)
        self._renew = renew or _renew_or_none
        self._db = db
        self._stop = threading.Event()
        self.lost = False
        self._thread = threading.Thread(target=self._run, name=f"lease-{job_id}", daemon=True)

    def start(self) -> "LeaseRenewer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        wait = self.interval
        while not self._stop.wait(wait):
            result = self._renew(self.job_id, self.worker_id)
            wait = self.retry_after_failure if result is None else self.interval
            # Close the connection after every renewal, not only return it to
            # the pool: behind pgbouncer in session mode an open client
            # connection holds a server connection, and idle renewer
            # connections would exhaust pgbouncer's pool, so later renewals
            # would wait out query_wait_timeout and live jobs lose their
            # leases. A renewal needs a connection for milliseconds.
            self._release_connection(discard=True)
            if result is False:
                self.lost = True
                logger.warning(f"[JOB {self.job_id}] Lease taken by another worker, stopping renewal")
                return

    def _release_connection(self, discard: bool = False) -> None:
        try:
            db = self._db or models_db
            if discard and hasattr(db, 'manual_close'):
                db.manual_close()           # pooled: close it for good instead of returning it
            elif not db.is_closed():
                db.close()                  # pooled: back to the pool
        except Exception:  # noqa: BLE001
            pass


def _renew_or_none(job_id: int, worker_id: str):
    """renew_job_lease, but None (retry later) instead of False when the
    database cannot be reached: only a row owned by another worker is a loss."""
    try:
        n = Job.update(leased_at=utcnow()).where(
            (Job.job_id == job_id) & (Job.worker_id == worker_id)).execute()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[JOB {job_id}] Lease renewal failed, retrying: {e}")
        return None
    return n > 0


def wait_out_dead_lease(job_id: int, worker_id: str, sleep, should_stop,
                        poll_s: float = 15.0, now=None) -> str:
    """Decide a REDELIVERED message whose job is leased by another worker.

    RabbitMQ redelivers an unacknowledged message when its consumer's
    connection dies, so the holder is most likely dead with its lease not yet
    expired: a killed pod's replacement gets the message within seconds, long
    before the LEASE_DURATION_SECONDS expiry. Discarding it would leave the job
    to the watchdog's 30-minute stall rule. A live
    holder renews every LEASE_RENEWAL_INTERVAL_SECONDS, so wait: a change of
    the lease row means the holder is alive (or someone else took the job).
    No change until the lease expires means it is dead, and the lease is taken.

    sleep(seconds) must keep the broker connection serviced (pika's
    connection.sleep). should_stop() is polled between sleeps.
    Returns 'acquired', 'alive' (drop the duplicate) or 'stopped' (shutdown
    requested, requeue the message). Raises if the database is unreachable.
    """
    now = now or utcnow
    try:
        first = Job.get(Job.job_id == job_id)
    except Exception as e:  # noqa: BLE001
        if is_transient_error(e):
            raise           # 'alive' would drop the message: retried later instead
        logger.error(f"[JOB {job_id}] Cannot read the lease row: {e}")
        return 'alive'
    seen = (first.worker_id, first.leased_at)
    if first.leased_at is None:
        return 'acquired' if acquire_job_lease(job_id, worker_id) else 'alive'
    deadline = first.leased_at + timedelta(seconds=LEASE_DURATION_SECONDS + 2 * poll_s)
    logger.warning(f"[JOB {job_id}] Redelivered while leased by {first.worker_id} "
                   f"(renewed {first.leased_at}), waiting for a renewal or the lease expiry")
    while True:
        if should_stop():
            return 'stopped'
        sleep(poll_s)
        try:
            row = Job.get(Job.job_id == job_id)
        except Exception as e:  # noqa: BLE001
            if is_transient_error(e):
                raise
            logger.error(f"[JOB {job_id}] Cannot read the lease row: {e}")
            return 'alive'
        if (row.worker_id, row.leased_at) != seen and row.worker_id is not None:
            logger.info(f"[JOB {job_id}] Lease renewed or taken by {row.worker_id}: holder alive")
            return 'alive'
        if acquire_job_lease(job_id, worker_id):
            logger.warning(f"[JOB {job_id}] Lease of {first.worker_id} expired unrenewed: taking over")
            return 'acquired'
        if now() > deadline:
            return 'alive'


def record_recovery_if_resumed(job: Job, is_redelivered: bool) -> bool:
    """Bump Job.recovery_count when this lease resumes a run
    that an earlier lease had already started.

    Call after the lease is acquired and the terminal/paused checks passed.
    `job` is the row as read before the lease. An earlier run is evident from
    started_at (set once the input is loaded) or a checkpointed epoch. This
    covers pod kills, evictions, watchdog republishes and lease takeovers. A
    RabbitMQ redelivery alone is not a recovery: a message redelivered before
    any worker started the job (a broker or consumer restart while the job was
    queued) had no work to resume, so counting it would give false positives.
    `is_redelivered` is only logged. A user resume after a
    pause also counts. The counter is never reset (unlike retry_count).
    Best-effort: a failure is logged and never interrupts the job.

    Returns True if the job was counted as recovered.
    """
    resumed = (job.started_at is not None
               or (job.current_epoch or 0) > 0)
    if not resumed:
        return False
    try:
        Job.update(
            recovery_count=Job.recovery_count + 1
        ).where(Job.job_id == job.job_id).execute()
        logger.warning(f"[JOB {job.job_id}] Resumed after an earlier lease "
                       f"(redelivered={is_redelivered}, epoch={job.current_epoch}), "
                       f"recovery_count incremented")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[JOB {job.job_id}] Could not increment recovery_count: {e}")
    return True


# Checkpoint/Resume Helpers

def calculate_last_snapshot_epoch(current_epoch: int, snapshot_interval: int) -> int:
    """The last interval snapshot at or before current_epoch: 0 if there is
    none, or if snapshot_interval is 0 (only the final epoch is saved)."""
    if snapshot_interval == 0:
        return 0

    if current_epoch < snapshot_interval:
        return 0

    return (current_epoch // snapshot_interval) * snapshot_interval


def should_save_snapshot(epoch: int, snapshot_interval: int, num_epochs: int) -> bool:
    """True if the epoch saves a snapshot: every snapshot_interval-th and the
    last (an interval of 0 saves only the last)."""
    if snapshot_interval == 0:
        return epoch == num_epochs

    return epoch % snapshot_interval == 0 or epoch == num_epochs


# CSV Loading Utilities

def load_bodies_from_csv(bucket: str, file_key: str) -> List[Dict]:
    """The bodies of a CSV object, with their accelerations (ax, ay, az) if the
    file has them. ValueError for an empty or malformed file."""
    raw_file = download_file(bucket, file_key)
    csv_reader = input_csv_reader(raw_file)
    all_bodies = []

    for row in csv_reader:
        body = {
            'n': int(row['n']),
            'idBody': int(row['idBody']),
            'm': float(row['m']),
            'x': float(row['x']),
            'y': float(row['y']),
            'z': float(row['z']),
            'vx': float(row['vx']),
            'vy': float(row['vy']),
            'vz': float(row['vz']),
        }
        # Include accelerations if present (for Velocity Verlet resumption)
        if 'ax' in row:
            body['ax'] = float(row['ax'])
            body['ay'] = float(row['ay'])
            body['az'] = float(row['az'])
        all_bodies.append(body)

    if len(all_bodies) == 0:
        raise ValueError(f"File {file_key} contains no bodies")

    return all_bodies


def load_checkpoint(job_id: int, epoch: int) -> List[Dict]:
    """The bodies of the job's snapshot at epoch, with their accelerations (ax,
    ay, az) if it has them. ValueError for an empty or invalid snapshot."""
    snapshot_key = f'job_id/{job_id}/epoch_num/{epoch}/epoch_results.csv'
    raw_file = download_file(EPOCH_RESULTS_BUCKET_NAME, snapshot_key)
    csv_reader = csv.DictReader(io.StringIO(raw_file))
    all_bodies = list(csv_reader)

    # Remove 'epoch' column (snapshots have it, original input doesn't)
    for body in all_bodies:
        body.pop('epoch', None)

    if len(all_bodies) == 0:
        raise ValueError("Empty snapshot - checkpoint file contains no bodies")

    required_keys = {'n', 'idBody', 'm', 'x', 'y', 'z', 'vx', 'vy', 'vz'}
    if not required_keys.issubset(all_bodies[0].keys()):
        raise ValueError(f"Missing keys: {required_keys - set(all_bodies[0].keys())}")

    has_accelerations = 'ax' in all_bodies[0]

    for body in all_bodies:
        body['n'] = int(body['n'])
        body['idBody'] = int(body['idBody'])
        body['m'] = float(body['m'])
        body['x'] = float(body['x'])
        body['y'] = float(body['y'])
        body['z'] = float(body['z'])
        body['vx'] = float(body['vx'])
        body['vy'] = float(body['vy'])
        body['vz'] = float(body['vz'])
        if has_accelerations:
            body['ax'] = float(body['ax'])
            body['ay'] = float(body['ay'])
            body['az'] = float(body['az'])

    return all_bodies


def validate_checkpoint_body_count(checkpoint_bodies: List[Dict], original_bucket: str, original_key: str) -> None:
    """ValueError unless the checkpoint has as many bodies as the input file. A
    storage error reading the input is raised as it is (the resume path
    re-raises retryable ones, not taken for a corrupt snapshot)."""
    # rows are only counted, not parsed: the check needs the count alone
    expected = sum(1 for _ in input_csv_reader(download_file(original_bucket, original_key)))
    if len(checkpoint_bodies) != expected:
        raise ValueError(
            f"Body count mismatch: expected {expected}, "
            f"got {len(checkpoint_bodies)}. Snapshot corrupted!"
        )


# Snapshot Management

def _snapshot_csv_from_arrays(
    epoch: int,
    ids: 'np.ndarray',
    masses: 'np.ndarray',
    positions: 'np.ndarray',
    velocities: 'np.ndarray',
    accelerations: 'np.ndarray | None' = None,
    ns: 'np.ndarray | None' = None,
) -> io.StringIO:
    """Build the snapshot CSV directly from numpy arrays.

    Column layout epoch, n, idBody, m, x, y, z, vx, vy, vz[, ax, ay, az],
    rows sorted by n, without converting bodies to per-row Python dicts.
    %.17g round-trips float64 exactly.
    """

    n = len(ids)
    if ns is None:
        ns = np.arange(n, dtype=np.int64)
    order = np.argsort(ns, kind='stable')

    header = 'epoch,n,idBody,m,x,y,z,vx,vy,vz'
    columns = [
        np.full(n, epoch, dtype=np.float64),
        ns[order].astype(np.float64),
        ids[order].astype(np.float64),
        masses[order],
        positions[order, 0], positions[order, 1], positions[order, 2],
        velocities[order, 0], velocities[order, 1], velocities[order, 2],
    ]
    fmt = ['%d', '%d', '%d'] + ['%.17g'] * 7
    if accelerations is not None:
        header += ',ax,ay,az'
        columns += [accelerations[order, 0], accelerations[order, 1], accelerations[order, 2]]
        fmt += ['%.17g'] * 3

    buffer = io.StringIO()
    buffer.write(header + '\r\n')
    np.savetxt(buffer, np.column_stack(columns), fmt=','.join(fmt), newline='\r\n')
    buffer.seek(0)
    return buffer


class SnapshotUploader:
    """Uploads snapshots on a background thread so S3 latency (and its
    retry sleeps) never stall the simulation loop.

    Bounded queue gives backpressure: if S3 is slower than the snapshot
    cadence, the sim loop blocks on submit() rather than queueing
    unbounded buffers in memory. drain() must be called before job
    completion — it propagates the first upload error so a failed
    snapshot upload fails the job (and triggers normal retry handling)
    instead of silently dropping data the resume/combine paths expect.

    A single instance is shared across jobs (see get_snapshot_uploader).
    An upload error is sticky until surfaced by submit()/drain(), then
    cleared so one job's failure doesn't poison the next. A job that ends
    without drain() (pause, cancel, lost lease, shutdown, failure) calls
    settle() instead, so its uploads and their error stay with it.
    """

    def __init__(self, max_pending: int = 2):
        self._queue = queue.Queue(maxsize=max_pending)
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name='snapshot-uploader')
        self._thread.start()

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            bucket, file_key, buffer = item
            try:
                # After a failure, skip the remaining queued uploads — the
                # job is going to fail at the next submit()/drain() anyway.
                if self._error is None:
                    upload_file(bucket, file_key, buffer)
            except Exception as e:  # surfaced via submit()/drain()
                logger.error(f'Background snapshot upload failed for {file_key}: {e}')
                self._error = e
            finally:
                self._queue.task_done()

    def _raise_pending_error(self):
        err = self._error
        if err is not None:
            self._error = None
            raise err

    def submit(self, bucket: str, file_key: str, buffer: io.StringIO) -> None:
        self._raise_pending_error()
        self._queue.put((bucket, file_key, buffer))

    def drain(self) -> None:
        """Block until all queued uploads finish. Raise the first error."""
        self._queue.join()
        self._raise_pending_error()

    def settle(self) -> None:
        """Block until all queued uploads finish. Log and drop an error."""
        self._queue.join()
        err, self._error = self._error, None
        if err is not None:
            logger.warning(f'Dropping the snapshot upload error of an ended job: {err}')

    def close(self) -> None:
        self._queue.put(None)


_snapshot_uploader: 'SnapshotUploader | None' = None


def get_snapshot_uploader() -> SnapshotUploader:
    """Worker-lifetime singleton uploader (one background thread, reused
    across jobs so exception paths can't leak threads)."""
    global _snapshot_uploader
    if _snapshot_uploader is None:
        _snapshot_uploader = SnapshotUploader()
    return _snapshot_uploader


def settle_snapshot_uploads() -> None:
    """At the end of every job: let its queued uploads land and drop their
    error, which would otherwise fail the next job's first submit()."""
    if _snapshot_uploader is not None:
        _snapshot_uploader.settle()


def upload_snapshot_from_arrays(
    job_id: int,
    epoch: int,
    ids,
    masses,
    positions,
    velocities,
    accelerations=None,
    ns=None,
):
    """Serialize a snapshot from numpy arrays and queue it for background upload."""
    buffer = _snapshot_csv_from_arrays(epoch, ids, masses, positions, velocities, accelerations, ns=ns)
    file_key = f'job_id/{job_id}/epoch_num/{epoch}/epoch_results.csv'
    get_snapshot_uploader().submit(EPOCH_RESULTS_BUCKET_NAME, file_key, buffer)
    logger.debug(f'[JOB {job_id}] Queued snapshot upload: epoch {epoch}')


def upload_snapshot_metadata(job_id: int, epoch: int, simulation_time: float, dt_used: float):
    """Upload the JSON sidecar of an adaptive job's snapshot: the simulation
    time at the epoch and the step it took, which the comparison service needs
    (an adaptive job's time is not epoch * dt)."""
    metadata = {
        "epoch": epoch,
        "simulation_time": simulation_time,
        "dt_used": dt_used,
    }
    metadata_key = f'job_id/{job_id}/epoch_num/{epoch}/metadata.json'
    buffer = io.BytesIO(json.dumps(metadata).encode('utf-8'))
    get_snapshot_uploader().submit(EPOCH_RESULTS_BUCKET_NAME, metadata_key, buffer)
    logger.debug(f'[JOB {job_id}] Queued adaptive metadata: epoch {epoch}, sim_time={simulation_time:.4e}, '
                 f'dt={dt_used:.4e}')


def load_snapshot_metadata(job_id: int, epoch: int):
    """The snapshot's sidecar ({'simulation_time', 'dt_used'}), None if it has
    none. Raises a retryable storage error (outage, throttling), which says
    nothing about whether the sidecar exists."""
    metadata_key = f'job_id/{job_id}/epoch_num/{epoch}/metadata.json'
    try:
        # download_file returns the decoded text, not a stream
        return json.loads(download_file(EPOCH_RESULTS_BUCKET_NAME, metadata_key))
    except Exception as e:
        if is_retryable_s3_error(e):
            raise
        logger.warning(f'[JOB {job_id}] No usable adaptive metadata for epoch {epoch}: {e}')
        return None


# Job Finalization

def final_snapshot_is_durable(job_id: int, num_epochs: int) -> bool:
    """True if the final epoch's snapshot is in object storage.

    The epoch loop records current_epoch = num_epochs before the uploader has
    drained, so a worker that dies (or whose final PUT fails) in between leaves
    a job that claims its last epoch without the result behind it. The
    finalize-only path must check before trusting the pointer. A missing
    object, or a check that fails for good (a denied request), counts as
    missing: the job is then recomputed from its last checkpoint, which costs
    time but never publishes a result that does not exist.

    A retryable storage error (outage, throttling) is raised: it says nothing
    about whether the snapshot exists, and rewinding the job on it would
    recompute an epoch that may well be stored."""
    key = f'job_id/{job_id}/epoch_num/{num_epochs}/epoch_results.csv'
    try:
        get_s3().meta.client.head_object(Bucket=EPOCH_RESULTS_BUCKET_NAME, Key=key)
        return True
    except Exception as e:
        if is_retryable_s3_error(e):
            raise
        logger.warning(f'[JOB {job_id}] Final snapshot {key} not readable: {e}')
        return False


def _record_completion(job: Job, num_epochs: int) -> None:
    """Count a completed job in the Prometheus totals (served on the worker's
    metrics port, the simulation-overview dashboard sums them). The body count
    is the job's input file's."""
    try:
        source = InputFile.get_or_none(InputFile.file_id == job.input_file_id) if job.input_file_id else None
        prometheus_metrics.record_job_completed(source.num_bodies if source else 0, num_epochs)
    except Exception as e:  # noqa: BLE001 — metrics only
        logger.debug(f'Could not record completion metrics: {e}')


def finalize_job(job: Job, message: MonolithicJobMessage, integration_completed_at=None):
    """Finalize the job: combine its snapshots or point to the final result.

    integration_completed_at: when the epoch loop and the final snapshot upload
    finished (UTC). Stored as completed_at, so durations (completed_at -
    started_at) end with the integration and exclude the post-processing below.
    None (finalize-only path after a crash during finalization) falls back to
    now.

    Any error is raised with the job left as it is: the caller's retry budget
    hands it back (the next worker takes the finalize-only path) and marks it
    FAILED only once the budget is spent, so one transient storage error does
    not fail a finished computation."""
    logger.info(f'[JOB {job.job_id}] Finalizing job...')

    try:
        if message.snapshot_interval > 0:
            logger.info(f'[JOB {job.job_id}] Combining snapshots...')
            results_file_key = combine_epoch_snapshots(
                job_id=job.job_id,
                snapshot_interval=message.snapshot_interval,
                num_epochs=message.num_of_epochs
            )
            logger.info(f'[JOB {job.job_id}] Combined results: {results_file_key}')
        else:
            results_file_key = f'job_id/{job.job_id}/epoch_num/{message.num_of_epochs}/epoch_results.csv'
            logger.info(f'[JOB {job.job_id}] Final result: {results_file_key}')

        # Use atomic update to set all fields at once. completed_at marks the
        # end of the integration (set together with COMPLETED so a job that
        # fails in post-processing has none). recovery_count is not touched.
        updated = Job.update(
            status=JobStatus.COMPLETED.value,
            current_epoch=message.num_of_epochs,
            results_file_key=results_file_key,
            updated_at=utcnow(),
            completed_at=integration_completed_at or utcnow(),
            retry_count=0,  # Reset retry budget on success (recovery_count keeps the history)
            last_retry_at=None
            # a cancel during the final combine is kept
        ).where((Job.job_id == job.job_id) & (Job.status != JobStatus.CANCELLED.value)).execute()

        if not updated:
            logger.info(f'[JOB {job.job_id}] Cancelled during finalization, the cancel is kept')
            return
        logger.info(f'[JOB {job.job_id}] Job finalized successfully')
        _record_completion(job, message.num_of_epochs)

    except Exception as e:
        logger.error(f'[JOB {job.job_id}] Finalization failed (will be retried): {e}', exc_info=True)
        raise


# Worker ID Generation

def generate_worker_id(suffix: str = "") -> str:
    """A unique worker id, with the optional suffix (e.g. "GPU")."""
    base_id = f"{socket.gethostname()}-{os.getpid()}"
    return f"{base_id}-{suffix}" if suffix else base_id


# Resume Logic

# A crash can leave the newest snapshots unwritten: current_epoch is written
# while they still wait in the background uploader (at most its queue and the
# upload in flight). Resume walks back over at most this many snapshot epochs.
# Finding none usable after that means storage lost data, and the job restarts
# from its input (slower, never wrong).
SNAPSHOT_FALLBACK_LIMIT = 8


def load_bodies_for_job(
    job: Job,
    message: MonolithicJobMessage,
    is_redelivered: bool,
    logger_instance
) -> Tuple[List[Dict], int]:
    """(all_bodies, start_epoch) of the job: its latest checkpoint for a
    resumed job (is_redelivered or progress recorded), else the input.

    Also sets job.current_simulation_time to the simulation time at the
    returned state (0 for the input), and for an adaptive job resumed from a
    snapshot job.resume_dt, the step its epoch took.

    ValueError if no bodies can be loaded. A retryable storage error is raised,
    so the job is retried instead of restarted from its input."""
    is_resume = is_redelivered or job.current_epoch > 0

    if not is_resume:
        # First run - load original input
        logger_instance.info(f'[JOB {job.job_id}] First run - starting from epoch 1')
        all_bodies = load_bodies_from_csv(message.bucket, message.file_key)
        job.current_simulation_time = 0.0
        return all_bodies, 1

    # Resume detected
    logger_instance.warning(f'[JOB {job.job_id}] RESUME DETECTED')
    logger_instance.warning(f'[JOB {job.job_id}]   - redelivered: {is_redelivered}')
    logger_instance.warning(f'[JOB {job.job_id}]   - current_epoch in DB: {job.current_epoch}')

    # Safety checks
    if job.status == JobStatus.COMPLETED.value:
        raise ValueError("Job already completed")

    if job.status in [JobStatus.FAILED.value, JobStatus.CANCELLED.value]:
        raise ValueError(f"Job in terminal state: {job.status}")

    if job.current_epoch >= message.num_of_epochs:
        raise ValueError("Job at final epoch - needs finalization only")

    last_snapshot_epoch = calculate_last_snapshot_epoch(
        job.current_epoch,
        message.snapshot_interval
    )

    logger_instance.info(f'[JOB {job.job_id}] Resume plan:')
    logger_instance.info(f'[JOB {job.job_id}]   - Last DB checkpoint: epoch {job.current_epoch:,}')
    logger_instance.info(f'[JOB {job.job_id}]   - Last snapshot: epoch {last_snapshot_epoch:,}')
    logger_instance.info(f'[JOB {job.job_id}]   - Total epochs: {message.num_of_epochs:,}')

    resumed = _newest_usable_snapshot(job, message, last_snapshot_epoch, logger_instance)
    if resumed is not None:
        all_bodies, epoch, simulation_time, dt_used = resumed
        logger_instance.info(f'[JOB {job.job_id}] Checkpoint loaded: epoch {epoch:,}, '
                             f'{len(all_bodies)} bodies (validated), simulation_time={simulation_time:.4e}')
        logger_instance.info(f'[JOB {job.job_id}] Will replay {job.current_epoch - epoch:,} epochs (fast-forward)')
        job.current_simulation_time = simulation_time
        if job.adaptive_enabled:
            # the step the checkpoint's epoch took: the loop continues its
            # step-size sequence from it. Not a column
            job.resume_dt = dt_used
        return all_bodies, epoch + 1

    # No valid checkpoint - restart from beginning
    if last_snapshot_epoch == 0:
        logger_instance.warning(f'[JOB {job.job_id}] No checkpoint available')
        logger_instance.warning(f'[JOB {job.job_id}] RECOMMENDATION: Set snapshot_interval > 0')
    else:
        logger_instance.warning(f'[JOB {job.job_id}] No usable snapshot at or before epoch '
                                f'{last_snapshot_epoch:,}, falling back to original input')
    logger_instance.warning(f'[JOB {job.job_id}] Progress lost: {job.current_epoch:,} epochs')

    all_bodies = load_bodies_from_csv(message.bucket, message.file_key)
    job.current_simulation_time = 0.0
    return all_bodies, 1


def _newest_usable_snapshot(job, message, last_snapshot_epoch: int, logger_instance):
    """(bodies, epoch, simulation_time, dt_used or None) of the newest
    readable snapshot at or before last_snapshot_epoch, or None.

    A missing or corrupt snapshot is skipped for the one before it. A
    retryable storage error propagates (an outage says nothing about the
    snapshot, and restarting from the input would throw the progress away).

    Simulation time at the snapshot: epoch * time_step for a fixed step. For
    an adaptive job the metadata sidecar's, and a snapshot with its sidecar is
    preferred (the sidecar is queued after its CSV, so the newest snapshot can
    lack it after a crash). Only when no candidate has one (runs from before
    sidecars existed) is the newest CSV used, with the time estimated as
    current_simulation_time * epoch / current_epoch from the database (exact
    for a constant step) and the loop starting from the initial step.
    """
    if last_snapshot_epoch <= 0 or message.snapshot_interval <= 0:
        return None
    epochs = list(range(last_snapshot_epoch, 0, -message.snapshot_interval))[:SNAPSHOT_FALLBACK_LIMIT]

    def readable(epoch):
        try:
            bodies = load_checkpoint(job.job_id, epoch)
            validate_checkpoint_body_count(bodies, message.bucket, message.file_key)
            return bodies
        except Exception as e:
            if is_retryable_s3_error(e):
                raise
            logger_instance.warning(f'[JOB {job.job_id}] Snapshot of epoch {epoch:,} not usable: {e}')
            return None

    if not job.adaptive_enabled:
        for epoch in epochs:
            bodies = readable(epoch)
            if bodies is not None:
                return bodies, epoch, epoch * message.time_step, None
        return None

    for epoch in epochs:
        meta = load_snapshot_metadata(job.job_id, epoch)
        if not meta or 'simulation_time' not in meta:
            continue
        bodies = readable(epoch)
        if bodies is not None:
            return bodies, epoch, float(meta['simulation_time']), meta.get('dt_used')
    for epoch in epochs:
        bodies = readable(epoch)
        if bodies is not None:
            db_epoch = job.current_epoch or 0
            db_time = job.current_simulation_time or 0.0
            estimate = db_time * epoch / db_epoch if db_epoch > 0 else 0.0
            logger_instance.warning(f'[JOB {job.job_id}] No snapshot metadata found, '
                                    f'simulation_time at epoch {epoch:,} estimated as {estimate:.4e}')
            return bodies, epoch, estimate, None
    return None
