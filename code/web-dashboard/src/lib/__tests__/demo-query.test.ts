// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import type { InputFileInfo, Job } from '@/types/api';
import { filterFiles, filterJobs, paginate } from '@/lib/demo/query';

const job = (id: number, extra: Partial<Job> = {}): Job =>
  ({
    job_id: id,
    status: 'COMPLETED',
    job_type: 'monolithic',
    integration_method: 'rk4',
    filename: `run_${id}.csv`,
    description: null,
    created_at: `2026-10-0${id}T00:00:00`,
    ...extra,
  }) as unknown as Job;

const file = (id: number, name: string, size: number, created: string): InputFileInfo =>
  ({
    file_id: id,
    user_friendly_name: name,
    description: null,
    file_size: size,
    created_at: created,
    last_used_at: created,
  }) as unknown as InputFileInfo;

describe('filterJobs', () => {
  const jobs = [job(1), job(2, { status: 'FAILED', description: 'Galaxy collision' }), job(3, { job_type: 'distributed_v2' })];

  it('sorts newest first', () => {
    expect(filterJobs(jobs, {}).map((j) => j.job_id)).toEqual([3, 2, 1]);
  });

  it('filters by status, type and search (text or exact id)', () => {
    expect(filterJobs(jobs, { status: 'FAILED' }).map((j) => j.job_id)).toEqual([2]);
    expect(filterJobs(jobs, { job_type: 'distributed_v2' }).map((j) => j.job_id)).toEqual([3]);
    expect(filterJobs(jobs, { search: 'galaxy' }).map((j) => j.job_id)).toEqual([2]);
    expect(filterJobs(jobs, { search: '1' }).map((j) => j.job_id)).toEqual([1]);
  });
});

describe('filterFiles', () => {
  const files = [file(1, 'beta', 10, '2026-10-01'), file(2, 'alpha', 30, '2026-10-03'), file(3, 'gamma', 20, '2026-10-02')];

  it('sorts by the requested key', () => {
    expect(filterFiles(files, {}).map((f) => f.file_id)).toEqual([2, 3, 1]);
    expect(filterFiles(files, { sort_by: 'name' }).map((f) => f.file_id)).toEqual([2, 1, 3]);
    expect(filterFiles(files, { sort_by: 'size' }).map((f) => f.file_id)).toEqual([2, 3, 1]);
  });

  it('does not reorder the input array', () => {
    filterFiles(files, { sort_by: 'name' });
    expect(files.map((f) => f.file_id)).toEqual([1, 2, 3]);
  });
});

describe('paginate', () => {
  const items = Array.from({ length: 45 }, (_, i) => i);

  it('slices pages and counts them', () => {
    const p = paginate(items, 3, 20);
    expect(p.items).toEqual([40, 41, 42, 43, 44]);
    expect(p).toMatchObject({ total: 45, page: 3, page_size: 20, pages: 3 });
  });

  it('clamps bad input like the backend', () => {
    expect(paginate(items, 0, 1000)).toMatchObject({ page: 1, page_size: 100, pages: 1 });
    expect(paginate([], 1, 20).pages).toBe(1);
  });

});
