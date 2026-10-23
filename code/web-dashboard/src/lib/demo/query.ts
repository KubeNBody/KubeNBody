// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Backend list semantics re-implemented over in-memory fixtures. Shared by the
// build-time api-server overlay and the runtime axios adapter, so prerendered
// pages and client-side refetches agree. Mirrors routes/jobs.py:list_jobs and
// routes/files.py:list_files.
import type { InputFileInfo, Job } from '@/types/api';
import { clampPage, clampPageSize, DEFAULT_PAGE_SIZE } from '@/lib/utils';

export interface DemoManifest {
  recorded_at: string;
  source_git_sha: string;
  chunk: number;
  job_ids: number[];
  comparison_ids: number[];
  file_ids: number[];
}

export interface DemoAnimationMeta {
  job_id: number;
  snapshot_interval: number;
  total_epochs: number;
  total_snapshots: number;
  chunk: number;
}

// Fixture layout under demo-data/ (written by scripts/export_demo_fixtures.py)
export const fixturePaths = {
  manifest: 'manifest.json',
  system: (name: string) => `system/${name}.json`,
  jobsIndex: 'jobs/index.json',
  job: (id: number) => `jobs/${id}/job.json`,
  jobPart: (id: number, part: 'progress' | 'status' | 'packages' | 'v2' | 'metrics' | 'summary') =>
    `jobs/${id}/${part}.json`,
  animationMeta: (id: number) => `jobs/${id}/anim/meta.json`,
  animationChunk: (id: number, k: number) => `jobs/${id}/anim/${k}.json`,
  selectable: 'selectable.json',
  compare: (refId: number) => `compare/${refId}.json`,
  comparisonsIndex: 'comparisons/index.json',
  comparison: (id: number) => `comparisons/${id}.json`,
  filesIndex: 'files/index.json',
  file: (id: number) => `files/${id}.json`,
  fileContent: (id: number) => `files/${id}.csv`,
} as const;

export interface JobFilters {
  status?: string;
  search?: string;
  job_type?: string;
  integration_method?: string;
}

const byCreatedDesc = <T extends { created_at: string }>(a: T, b: T) =>
  b.created_at.localeCompare(a.created_at);

export function filterJobs(jobs: Job[], filters: JobFilters): Job[] {
  const term = filters.search?.trim().toLowerCase() ?? '';
  return jobs
    .filter((job) => {
      if (filters.status && job.status !== filters.status) return false;
      if (filters.job_type && job.job_type !== filters.job_type) return false;
      if (filters.integration_method && job.integration_method !== filters.integration_method) return false;
      if (term) {
        const matchesText =
          (job.filename ?? '').toLowerCase().includes(term) ||
          (job.name ?? '').toLowerCase().includes(term) ||
          (job.description ?? '').toLowerCase().includes(term);
        const matchesId = /^\d+$/.test(term) && job.job_id === Number(term);
        if (!matchesText && !matchesId) return false;
      }
      return true;
    })
    .sort(byCreatedDesc);
}

export interface FileFilters {
  search?: string;
  sort_by?: string;
}

export function filterFiles(files: InputFileInfo[], filters: FileFilters): InputFileInfo[] {
  const term = filters.search?.toLowerCase() ?? '';
  const matched = term
    ? files.filter(
        (f) =>
          f.user_friendly_name.toLowerCase().includes(term) ||
          (f.description ?? '').toLowerCase().includes(term)
      )
    : [...files];

  switch (filters.sort_by) {
    case 'name':
      return matched.sort((a, b) => a.user_friendly_name.localeCompare(b.user_friendly_name));
    case 'last_used_at':
      return matched.sort((a, b) => b.last_used_at.localeCompare(a.last_used_at));
    case 'size':
      return matched.sort((a, b) => b.file_size - a.file_size);
    default:
      return matched.sort(byCreatedDesc);
  }
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export function paginate<T>(items: T[], page = 1, pageSize = DEFAULT_PAGE_SIZE): Page<T> {
  const safePage = clampPage(page);
  const safeSize = clampPageSize(pageSize);
  const start = (safePage - 1) * safeSize;
  return {
    items: items.slice(start, start + safeSize),
    total: items.length,
    page: safePage,
    page_size: safeSize,
    pages: items.length > 0 ? Math.ceil(items.length / safeSize) : 1,
  };
}

// Pages that read the URL accept positive integers only (useUrlFilters does the same)
