// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Demo implementation of ServerApi (../types): the static demo build swaps it in
// for lib/data/server.ts (next.config.js). Data comes from the recorded fixtures
// in demo-data/, read from disk while `next build` prerenders the server
// components. No cookies(), no network - the pages become plain static HTML with
// their data inlined.
import type { InputFileInfo, Job, JobPackage, JobProgress } from '@/types/api';
import { APIServerError } from '@/lib/api-error';
import { parseJobId } from '@/lib/utils';
import type { FileListParams, JobListParams } from '../types';
import { readFixture as readFixtureFile } from '@/lib/data/demo/fixtures-fs';
import { filterFiles, filterJobs, fixturePaths, paginate } from '@/lib/demo/query';

export { APIServerError };

async function readFixture<T>(relativePath: string): Promise<T> {
  try {
    return await readFixtureFile<T>(relativePath);
  } catch {
    throw new APIServerError(`Demo fixture not found: ${relativePath}`, 404);
  }
}

function jobId(id: string): number {
  const n = parseJobId(id);
  if (n === null) {
    throw new APIServerError(`Invalid job ID: "${id}"`, 400);
  }
  return n;
}

export async function getJobs(params: JobListParams = {}): Promise<{ jobs: Job[]; total: number; page: number; size: number; pages: number }> {
  const jobs = await readFixture<Job[]>(fixturePaths.jobsIndex);
  const { items, total, page, page_size, pages } = paginate(filterJobs(jobs, params), params.page, params.size);
  return { jobs: items, total, page, size: page_size, pages };
}

export async function getJob(id: string): Promise<Job> {
  return readFixture(fixturePaths.job(jobId(id)));
}

export async function getJobProgress(id: string): Promise<JobProgress> {
  return readFixture(fixturePaths.jobPart(jobId(id), 'progress'));
}

export async function getJobPackages(id: string): Promise<JobPackage[]> {
  return readFixture(fixturePaths.jobPart(jobId(id), 'packages'));
}

export async function getFiles(params: FileListParams = {}): Promise<{ files: InputFileInfo[]; total: number; page: number; size: number; pages: number }> {
  const files = await readFixture<InputFileInfo[]>(fixturePaths.filesIndex);
  const { items, total, page, page_size, pages } = paginate(filterFiles(files, params), params.page, params.size);
  return { files: items, total, page, size: page_size, pages };
}
