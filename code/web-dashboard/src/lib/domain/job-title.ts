// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// What a job is called in lists and headers: its name if it has one, else its
// input file, else its number.

/** Longest job name the API accepts (code/src/constants.py JOB_NAME_MAX) */
export const JOB_NAME_MAX = 120;

export function jobTitle(job: { job_id: number; name?: string | null; filename?: string | null }): string {
  return job.name?.trim() || job.filename || `Job ${job.job_id}`;
}
