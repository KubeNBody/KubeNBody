# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Recovering a V2 job with its PostgreSQL bookkeeping: the API's recover
endpoint and a worker that finds an epoch's staged state lost both go
through recover_v2_job."""

from typing import Any, Optional

from logging_config import setup_logging
from models import Job, utcnow
from schemas import JobStatus

from .orchestrator_v2 import RecoveryConflict, get_orchestrator


logger = setup_logging('v2-recovery')


def recover_v2_job(job_id: int, pg_job: Any, if_generation: Optional[int] = None) -> Optional[int]:
    """Recover the job from its latest snapshot (OrchestratorV2.recover_job,
    with pg_job as the metadata fallback) and record the recovery in
    PostgreSQL. Returns the recovered epoch, or None if nothing to recover
    from was found. Raises what recover_job raises (RecoveryConflict, the
    object store's errors)."""
    status_before = pg_job.status
    orchestrator = get_orchestrator()

    def record_recovery(recovered_epoch: int) -> None:
        """Record the recovery in PostgreSQL. Called by the recovery once the
        Redis state is restored and before any of it is queued, so no worker
        can finalize the recovered job before its row is running again.

        Column updates only (job.save() would rewrite the whole row with what
        we read before the recovery). The counter is incremented in the
        database, so concurrent recoveries all count."""
        Job.update(current_epoch=recovered_epoch, updated_at=utcnow(), completed_at=None,
                   recovery_count=Job.recovery_count + 1).where(Job.job_id == job_id).execute()
        # The status unless a cancel or pause changed it during the recovery:
        # both went to Redis first, and the recovered job follows them there
        # (a pause stops it after its first epoch). Any other status (e.g. the
        # watchdog's FAILED for the lost state) is the old run's.
        held = {JobStatus.CANCELLED.value, JobStatus.PAUSED.value} - {status_before}
        if not Job.update(status=JobStatus.IN_PROGRESS.value).where(
                (Job.job_id == job_id) & (Job.status.not_in(tuple(held)))).execute():
            current = Job.get_or_none(Job.job_id == job_id)
            if current is not None and current.status == JobStatus.CANCELLED.value:
                # e.g. a cancel that found no Redis meta while it was rebuilt:
                # applied to Redis too. The recovery queues nothing
                orchestrator.cancel_job(job_id)
                raise RecoveryConflict(f"Job {job_id} was cancelled during its recovery")
            logger.info(f"Job {job_id} recovered, its status changed meanwhile "
                        f"({getattr(current, 'status', None)}) and is kept")

    return orchestrator.recover_job(job_id, pg_job=pg_job, on_restored=record_recovery,
                                    if_generation=if_generation)
