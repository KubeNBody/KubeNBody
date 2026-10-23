// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use server';

import { backendAction, invalidId } from '@/lib/data/server';
import type { Job } from '@/types/api';

const isV2 = (jobType?: string) => jobType === 'distributed_v2';

// Pages that show a job's status
const jobPages = (jobId: number) => ['/dashboard/jobs', '/dashboard', `/dashboard/jobs/${jobId}`];

export async function cancelJob(jobId: number, jobType?: string) {
  const bad = invalidId(jobId, 'job ID');
  if (bad) return bad;
  // V2 jobs use a different cancel endpoint
  const [endpoint, method] = isV2(jobType)
    ? [`/api/v2/jobs/${jobId}/cancel`, 'POST']
    : [`/api/jobs/${jobId}`, 'DELETE'];
  return backendAction(endpoint, { method }, {
    fallbackError: 'Failed to cancel job',
    revalidate: jobPages(jobId),
  });
}

export async function pauseJob(jobId: number, jobType?: string) {
  const bad = invalidId(jobId, 'job ID');
  if (bad) return bad;
  const endpoint = isV2(jobType) ? `/api/v2/jobs/${jobId}/pause` : `/api/jobs/${jobId}/pause`;
  return backendAction(endpoint, { method: 'POST' }, {
    fallbackError: 'Failed to pause job',
    revalidate: jobPages(jobId),
  });
}

export async function resumeJob(jobId: number, jobType?: string) {
  const bad = invalidId(jobId, 'job ID');
  if (bad) return bad;
  const endpoint = isV2(jobType) ? `/api/v2/jobs/${jobId}/resume` : `/api/jobs/${jobId}/resume`;
  return backendAction(endpoint, { method: 'POST' }, {
    fallbackError: 'Failed to resume job',
    revalidate: jobPages(jobId),
  });
}

export async function deleteJob(jobId: number) {
  const bad = invalidId(jobId, 'job ID');
  if (bad) return bad;
  // The backend hands V2 jobs to the V2 permanent delete (Redis, S3, record)
  return backendAction(`/api/jobs/${jobId}/permanent`, { method: 'DELETE' }, {
    fallbackError: 'Failed to delete job',
    revalidate: jobPages(jobId),
  });
}

// V2 only. A COMPLETED job is refused (409) unless `force`: it is then rerun
// from its latest snapshot (src/v2/api_v2.py recover_job)
export async function recoverJob(jobId: number, force = false) {
  const bad = invalidId(jobId, 'job ID');
  if (bad) return bad;
  return backendAction(`/api/v2/jobs/${jobId}/recover${force ? '?force=true' : ''}`, { method: 'POST' }, {
    fallbackError: 'Failed to recover job',
    revalidate: jobPages(jobId),
  });
}

// The job's short title: null (or an empty string) clears it
export async function updateJobName(jobId: number, name: string | null) {
  const bad = invalidId(jobId, 'job ID');
  if (bad) return bad;
  return backendAction<Job>(`/api/jobs/${jobId}/name`, {
    method: 'PATCH',
    body: JSON.stringify({ name: name?.trim() ? name.trim() : null }),
  }, {
    fallbackError: 'Failed to save the name',
    revalidate: jobPages(jobId),
  });
}

// The job's free-text note: null (or an empty string) clears it
export async function updateJobDescription(jobId: number, description: string | null) {
  const bad = invalidId(jobId, 'job ID');
  if (bad) return bad;
  return backendAction<Job>(`/api/jobs/${jobId}/description`, {
    method: 'PATCH',
    body: JSON.stringify({ description: description?.trim() ? description : null }),
  }, {
    fallbackError: 'Failed to save the description',
    revalidate: jobPages(jobId),
  });
}
