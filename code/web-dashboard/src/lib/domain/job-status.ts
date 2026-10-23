// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { Activity, Ban, CheckCircle, Clock, Pause, XCircle, type LucideIcon } from 'lucide-react';
import type { JobStatus } from '@/types/api';
import { TONE_BADGE, TONE_TEXT, TONE_TILE, type Tone } from './tones';

// Display config for every job status the backend reports (src/enums.py JobStatus)
export interface JobStatusInfo {
  icon: LucideIcon;
  tone: Tone;
  label: string;
  /** icon/text colour */
  color: string;
  /** icon tile background */
  bgColor: string;
  /** full pill classes: background, text, border */
  badge: string;
}

const status = (icon: LucideIcon, tone: Tone, label: string): JobStatusInfo => ({
  icon,
  tone,
  label,
  color: TONE_TEXT[tone],
  bgColor: TONE_TILE[tone],
  badge: TONE_BADGE[tone],
});

export const statusConfig: Record<JobStatus, JobStatusInfo> = {
  PENDING: status(Clock, 'caution', 'Pending'),
  IN_PROGRESS: status(Activity, 'info', 'Running'),
  PAUSED: status(Pause, 'warning', 'Paused'),
  COMPLETED: status(CheckCircle, 'success', 'Completed'),
  FAILED: status(XCircle, 'danger', 'Failed'),
  CANCELLED: status(Ban, 'neutral', 'Cancelled'),
};

/** Status info for any string the backend may send (unknown -> neutral, raw label) */
export function jobStatusInfo(value: string): JobStatusInfo {
  return statusConfig[value as JobStatus] ?? status(Clock, 'neutral', value);
}

// Lifecycle transitions the backend accepts (routes/jobs.py and v2/api_v2.py,
// which agree): the list and details pages both gate their buttons on these
export const canCancelStatus = (status: string) =>
  status === 'PENDING' || status === 'IN_PROGRESS' || status === 'PAUSED';
export const canPauseStatus = (status: string) =>
  status === 'PENDING' || status === 'IN_PROGRESS';
export const canResumeStatus = (status: string) => status === 'PAUSED';
export const canDeleteStatus = (status: string) =>
  status === 'COMPLETED' || status === 'FAILED' || status === 'CANCELLED';
// V2 recovery restarts a job from its latest snapshot (src/v2/api_v2.py
// recover_job). The pause/resume 409s for a job whose state was lost tell the
// user to recover it, so it is offered for every unfinished state and FAILED.
// The backend refuses a CANCELLED job, and reruns a COMPLETED one only with
// force=true (see canForceRecoverStatus).
export const canRecoverStatus = (jobType: string, status: string) =>
  jobType === 'distributed_v2' && (status === 'FAILED' || status === 'PAUSED' || status === 'IN_PROGRESS');
export const canForceRecoverStatus = (jobType: string, status: string) =>
  jobType === 'distributed_v2' && status === 'COMPLETED';
// Recovering anything but a FAILED job throws work away (a running or paused
// job rolls back, a completed one reruns), so the details page confirms it
// first with this prompt (null: one click)
export const recoverConfirmPrompt = (status: string): string | null => {
  switch (status) {
    case 'IN_PROGRESS':
    case 'PAUSED':
      return 'Recover? This rolls the job back to its latest snapshot, and the progress since then is recomputed.';
    case 'COMPLETED':
      return 'Rerun this completed job from its latest snapshot?';
    default:
      return null;
  }
};

// Snapshots up to lastEpoch, as the backend counts them (src/utils/job_helpers.py
// snapshot_epochs): every snapshotInterval-th epoch, plus lastEpoch itself when
// includeLast. An interval of 0 or less leaves only lastEpoch.
export const snapshotCount = (lastEpoch: number, snapshotInterval: number, includeLast: boolean): number => {
  if (lastEpoch <= 0) return 0;
  if (snapshotInterval <= 0) return includeLast ? 1 : 0;
  const every = Math.floor(lastEpoch / snapshotInterval);
  return every + (includeLast && lastEpoch % snapshotInterval ? 1 : 0);
};

// Whether the animation endpoint (src/routes/results.py) has something to play:
// a completed monolithic or V1 job always, a running one once a snapshot exists.
// V2 needs two snapshots and writes none at epoch 0, so a V2 job of at most one
// snapshot interval has none to animate.
export const canViewAnimationFor = (job: {
  job_type: string;
  status: string;
  current_epoch: number;
  num_epochs: number;
  snapshot_interval: number;
}): boolean => {
  const completed = job.status === 'COMPLETED';
  if (job.job_type === 'distributed_v2') {
    return completed
      ? snapshotCount(job.num_epochs, job.snapshot_interval, true) >= 2
      : snapshotCount(job.current_epoch, job.snapshot_interval, false) >= 2;
  }
  return completed || snapshotCount(job.current_epoch, job.snapshot_interval, false) >= 1;
};
