// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Short names for the jobs of a comparison: each job is named by what it does
// differently from the reference (another integrator, the tree, the
// distributed engine, another step), the reference by its own value of
// everything that varies, so "#41 vs #42, #43" reads
// "#41 Velocity Verlet vs #42 Simple Euler, #43 RK4".
import { forceAlgorithmLabel, jobTypeLabel } from '@/lib/domain/badges';
import { integrationMethodLabel } from '@/lib/format-integration';
import type { JobComparisonSummary, JobToJobComparisonResponse } from '@/types/api';

type Setup = Pick<JobComparisonSummary, 'job_type' | 'force_algorithm' | 'integration_method' | 'time_step' | 'softening_sq' | 'gpu_enabled'>;

// In display order. `value` normalises defaults so a missing field equals its default
const DIMENSIONS: { value: (j: Setup) => string; label: (j: Setup) => string }[] = [
  { value: (j) => j.job_type, label: (j) => jobTypeLabel(j.job_type) },
  { value: (j) => j.force_algorithm || 'direct', label: (j) => forceAlgorithmLabel(j.force_algorithm) },
  { value: (j) => j.integration_method || 'simple_euler', label: (j) => integrationMethodLabel(j.integration_method) },
  { value: (j) => String(j.time_step), label: (j) => `dt ${j.time_step}` },
  { value: (j) => String(j.softening_sq ?? ''), label: (j) => `ε² ${j.softening_sq}` },
  { value: (j) => (j.gpu_enabled ? 'gpu' : 'cpu'), label: (j) => (j.gpu_enabled ? 'GPU' : 'CPU') },
];

export interface ComparisonLabels {
  /** The reference's values of every dimension that varies ('' if none does) */
  reference: string;
  /** Per compared job: what differs from the reference ('' if nothing does) */
  jobs: Record<number, string>;
}

export function comparisonLabels(reference: Setup, jobs: (Setup & { job_id: number })[]): ComparisonLabels {
  const varying = DIMENSIONS.filter((d) => jobs.some((j) => d.value(j) !== d.value(reference)));
  return {
    reference: varying.map((d) => d.label(reference)).join(' · '),
    jobs: Object.fromEntries(
      jobs.map((j) => [j.job_id, varying.filter((d) => d.value(j) !== d.value(reference)).map((d) => d.label(j)).join(' · ')])
    ),
  };
}

export function labelsFromResults(results: JobToJobComparisonResponse): ComparisonLabels {
  return comparisonLabels(
    results.reference_job_info,
    results.comparisons.map((c) => ({ ...c.job_info, job_id: c.job_id }))
  );
}

/** "#41 Velocity Verlet" or just "#41" */
export function jobWithLabel(jobId: number, label: string | undefined): string {
  return label ? `#${jobId} ${label}` : `#${jobId}`;
}

/** "#41 Velocity Verlet vs #42 Simple Euler, #43 RK4" */
export function comparisonTitle(results: JobToJobComparisonResponse): string {
  const labels = labelsFromResults(results);
  const others = results.comparisons.map((c) => jobWithLabel(c.job_id, labels.jobs[c.job_id]));
  return `${jobWithLabel(results.reference_job_id, labels.reference)} vs ${others.join(', ')}`;
}
