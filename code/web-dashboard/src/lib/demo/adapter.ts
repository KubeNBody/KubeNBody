// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Axios adapter for the static demo build: answers the dashboard's /api/* calls
// from the recorded fixtures under ${BASE_PATH}/demo-data/ instead of the network.
// Installed by api-client.ts only when IS_DEMO.
import { AxiosError, AxiosHeaders, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios';
import type {
  ComparisonJobResponse,
  InputFileInfo,
  Job,
  JobComparisonSummary,
  JobToJobComparisonResponse,
} from '@/types/api';
import type { AnimationSnapshot } from '@/types/api';
import { DEMO_DATA_URL, DEMO_READ_ONLY_MESSAGE } from '@/lib/demo';
import {
  type DemoAnimationMeta,
  filterJobs,
  fixturePaths,
  paginate,
} from './query';
import { parsePageParams } from '@/lib/utils';

/** paginate's page and page size from the request's query values */
function pageOf(page: string | null, size: string | null): [number, number] {
  const p = parsePageParams(page, size);
  return [p.page, p.size];
}

class DemoHttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const cache = new Map<string, Promise<unknown>>();

function loadJson<T>(path: string): Promise<T> {
  let entry = cache.get(path);
  if (!entry) {
    entry = fetch(`${DEMO_DATA_URL}/${path}`).then((res) => {
      if (res.status === 404) throw new DemoHttpError(404, 'Not found');
      if (!res.ok) throw new DemoHttpError(res.status, `Failed to load demo data (${res.status})`);
      return res.json();
    });
    // Don't cache failures - a flaky network shouldn't poison the session
    entry.catch(() => cache.delete(path));
    cache.set(path, entry);
  }
  return entry as Promise<T>;
}

type Query = URLSearchParams;
type Handler = (match: RegExpMatchArray, query: Query, body: any) => Promise<unknown>;

const id = (m: RegExpMatchArray, i = 1) => Number(m[i]);

async function animationPage(jobId: number, query: Query) {
  const meta = await loadJson<DemoAnimationMeta>(fixturePaths.animationMeta(jobId));
  const offset = Math.max(0, Number(query.get('offset')) || 0);
  const limit = Math.min(100, Math.max(1, Number(query.get('limit')) || 20));

  if (offset > 0 && offset >= meta.total_snapshots) {
    throw new DemoHttpError(400, `Offset ${offset} exceeds available snapshots (${meta.total_snapshots})`);
  }

  const end = Math.min(offset + limit, meta.total_snapshots);
  const chunks: AnimationSnapshot[][] = [];
  for (let k = Math.floor(offset / meta.chunk); k * meta.chunk < end; k++) {
    chunks.push(await loadJson<AnimationSnapshot[]>(fixturePaths.animationChunk(jobId, k)));
  }
  const firstChunkStart = Math.floor(offset / meta.chunk) * meta.chunk;
  const snapshots = chunks.flat().slice(offset - firstChunkStart, end - firstChunkStart);

  return {
    job_id: jobId,
    snapshot_interval: meta.snapshot_interval,
    total_epochs: meta.total_epochs,
    total_snapshots: meta.total_snapshots,
    offset,
    limit,
    has_next: offset + snapshots.length < meta.total_snapshots,
    snapshots,
  };
}

// The backend compares each job against the reference independently, so a
// "reference vs every other job" recording answers any subset of it.
// JobToJobComparisonRequest.comparison_job_ids max_length on the backend
const MAX_COMPARISON_JOBS = 5;

async function compare(body: any): Promise<JobToJobComparisonResponse> {
  const refId = Number(body?.reference_job_id);
  const requested: number[] = (body?.comparison_job_ids ?? []).map(Number);
  if (requested.length > MAX_COMPARISON_JOBS) {
    throw new DemoHttpError(422, `Compare at most ${MAX_COMPARISON_JOBS} jobs at a time`);
  }
  const wanted = new Set<number>(requested);
  const full = await loadJson<JobToJobComparisonResponse>(fixturePaths.compare(refId));
  return {
    ...full,
    comparisons: full.comparisons.filter((c) => wanted.has(c.job_id)),
    skipped_jobs: full.skipped_jobs.filter((s) => wanted.has(s.job_id)),
  };
}

const getRoutes: Array<[RegExp, Handler]> = [
  [/^\/api\/health$/, () => loadJson(fixturePaths.system('health'))],
  [/^\/api\/metrics$/, () => loadJson(fixturePaths.system('metrics'))],
  [/^\/api\/ready$/, () => loadJson(fixturePaths.system('ready'))],
  [/^\/api\/v2\/health$/, () => loadJson(fixturePaths.system('v2-health'))],
  [/^\/api\/v2\/stats$/, () => loadJson(fixturePaths.system('v2-stats'))],
  [/^\/api\/comparison\/cache\/stats$/, () => loadJson(fixturePaths.system('cache-stats'))],

  [/^\/api\/jobs$/, async (_m, q) => {
    const jobs = await loadJson<Job[]>(fixturePaths.jobsIndex);
    const filtered = filterJobs(jobs, {
      status: q.get('status') ?? undefined,
      search: q.get('search') ?? undefined,
      job_type: q.get('job_type') ?? undefined,
      integration_method: q.get('integration_method') ?? undefined,
    });
    const { items, ...page } = paginate(
      filtered,
      ...pageOf(q.get('page'), q.get('page_size') ?? q.get('size'))
    );
    return { jobs: items, ...page, size: page.page_size };
  }],
  [/^\/api\/jobs\/selectable$/, async (_m, q) => {
    const jobs = await loadJson<Array<{ status: string }>>(fixturePaths.selectable);
    const status = q.get('status');
    return status ? jobs.filter((j) => j.status === status) : jobs;
  }],
  [/^\/api\/jobs\/(\d+)$/, (m) => loadJson(fixturePaths.job(id(m)))],
  [/^\/api\/jobs\/(\d+)\/progress$/, (m) => loadJson(fixturePaths.jobPart(id(m), 'progress'))],
  [/^\/api\/jobs\/(\d+)\/status$/, (m) => loadJson(fixturePaths.jobPart(id(m), 'status'))],
  [/^\/api\/jobs\/(\d+)\/packages$/, (m) => loadJson(fixturePaths.jobPart(id(m), 'packages'))],
  [/^\/api\/v2\/jobs\/(\d+)$/, (m) => loadJson(fixturePaths.jobPart(id(m), 'v2'))],
  [/^\/api\/jobs\/(\d+)\/animation-data\/paginated$/, (m, q) => animationPage(id(m), q)],

  [/^\/api\/comparison\/jobs\/(\d+)\/metrics$/, (m) => loadJson(fixturePaths.jobPart(id(m), 'metrics'))],
  [/^\/api\/comparison\/jobs\/(\d+)\/summary$/, (m) => loadJson(fixturePaths.jobPart(id(m), 'summary'))],
  [/^\/api\/comparison\/jobs\/summaries$/, async (_m, q) => {
    const ids = (q.get('job_ids') ?? '').split(',').filter(Boolean).map(Number);
    const summaries = await Promise.all(
      ids.map((jobId) =>
        loadJson<JobComparisonSummary>(fixturePaths.jobPart(jobId, 'summary')).catch(() => null)
      )
    );
    return summaries.filter(Boolean);
  }],
  [/^\/api\/comparison\/saved$/, async (_m, q) => {
    const all = await loadJson<{ comparisons: ComparisonJobResponse[] }>(fixturePaths.comparisonsIndex);
    const status = q.get('status');
    const matching = status ? all.comparisons.filter((c) => c.status === status) : all.comparisons;
    const { items, ...page } = paginate(matching, ...pageOf(q.get('page'), q.get('page_size')));
    return { comparisons: items, ...page };
  }],
  [/^\/api\/comparison\/saved\/(\d+)$/, (m) => loadJson(fixturePaths.comparison(id(m)))],

  [/^\/api\/files\/input$/, async () => {
    const files = await loadJson<InputFileInfo[]>(fixturePaths.filesIndex);
    const sorted = [...files].sort((a, b) => b.last_used_at.localeCompare(a.last_used_at));
    return { files: sorted, total: sorted.length, page: 1, page_size: sorted.length };
  }],
];

// The only POST that reads rather than writes
const postRoutes: Array<[RegExp, Handler]> = [
  [/^\/api\/comparison\/compare$/, (_m, _q, body) => compare(body)],
];

function parseRequest(config: InternalAxiosRequestConfig) {
  const url = new URL(config.url ?? '', 'http://demo.invalid');
  const query = new URLSearchParams(url.search);
  for (const [key, value] of Object.entries(config.params ?? {})) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  let body = config.data;
  if (typeof body === 'string') {
    try {
      body = JSON.parse(body);
    } catch {
      // leave as-is
    }
  }
  return { path: url.pathname, query, body, method: (config.method ?? 'get').toLowerCase() };
}

function respond<T>(config: InternalAxiosRequestConfig, status: number, data: T): AxiosResponse<T> {
  return {
    data,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: new AxiosHeaders({ 'content-type': 'application/json' }),
    config,
    request: {},
  };
}

export async function demoAdapter(config: InternalAxiosRequestConfig): Promise<AxiosResponse> {
  const { path, query, body, method } = parseRequest(config);
  const routes = method === 'get' ? getRoutes : method === 'post' ? postRoutes : [];

  try {
    for (const [pattern, handler] of routes) {
      const match = path.match(pattern);
      if (match) {
        return respond(config, 200, await handler(match, query, body));
      }
    }
    throw method === 'get'
      ? new DemoHttpError(404, 'Not available in the demo')
      : new DemoHttpError(403, DEMO_READ_ONLY_MESSAGE);
  } catch (error) {
    const status = error instanceof DemoHttpError ? error.status : 500;
    const detail = error instanceof Error ? error.message : 'Demo data error';
    const response = respond(config, status, { detail });
    // Same error shape as a real HTTP failure, so api-client's interceptor maps it to APIError
    throw new AxiosError(detail, AxiosError.ERR_BAD_REQUEST, config, {}, response);
  }
}
