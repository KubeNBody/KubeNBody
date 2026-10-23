# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Set Job.started_at / Job.completed_at (migration 020).

started_at is written once, by whichever worker first starts computing the job
(conditional on it still being NULL, so a restarted or recovered job keeps its
original start). Best-effort: a failure is logged and never interrupts the
computation. completed_at is set by the code that marks the job COMPLETED. The
monolithic worker stores the time its epoch loop and final snapshot upload
finished, so snapshot combination is excluded.

wait_for_job_schema covers every column the Job model maps, including
recovery_count (migration 021).
"""
import json
import time

from logging_config import setup_logging
from models import Job, db, utcnow


logger = setup_logging('job-timestamps')


def mark_job_started(job_id: int) -> bool:
    """Record the start time if none is recorded yet. Returns True if set now."""
    try:
        n = Job.update(started_at=utcnow()).where(
            (Job.job_id == job_id) & (Job.started_at.is_null())
        ).execute()
        return bool(n)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not record started_at for job {job_id}: {e}")
        return False


def record_compute_hosts(job_id: int, hosts) -> bool:
    """Add the hosts that computed a job (utils.metadata.compute_host() dicts)
    to its metadata_json under "compute_hosts", without duplicates. A
    read-modify-write: call it from one writer at a time (the monolithic worker
    that holds the lease, or the V2 finalizer). Best-effort, like the marks."""
    try:
        job = Job.get_or_none(Job.job_id == job_id)
        if job is None:
            return False
        meta = json.loads(job.metadata_json) if job.metadata_json else {}
        known = meta.get("compute_hosts") or []
        for h in hosts:
            if h not in known:
                known.append(h)
        meta["compute_hosts"] = known
        Job.update(metadata_json=json.dumps(meta)).where(Job.job_id == job_id).execute()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not record the compute hosts of job {job_id}: {e}")
        return False


def wait_for_job_schema(poll_s: float = 10.0, timeout_s: float = None) -> bool:
    """Block until the job table has every column the Job model maps.

    Peewee selects all model fields, so a worker running a newer image than
    the database schema fails every Job query (and would drop tasks or mark
    finished jobs FAILED). The API applies migrations at startup. Workers call
    this before consuming, which makes the rollout order-independent: new
    workers simply wait for the API's migration instead of failing.

    Returns True once the schema is ready, False if timeout_s elapses.
    """
    start = time.time()
    while True:
        try:
            wanted = {f.column_name for f in Job._meta.sorted_fields}
            present = {c.name for c in db.get_columns(Job._meta.table_name)}
            missing = sorted(wanted - present)
            if not missing:
                return True
            logger.warning(f"Job table lacks columns {missing} (migration pending), "
                           f"waiting for the API to migrate")
        except Exception as e:  # noqa: BLE001 — database not reachable yet
            logger.warning(f"Could not inspect the job table: {e}")
        if timeout_s is not None and time.time() - start >= timeout_s:
            return False
        time.sleep(poll_s)
