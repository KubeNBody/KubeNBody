// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import 'server-only';
import { Job, JobPackage, JobProgress, InputFileInfo } from '@/types/api';
import { revalidatePath } from 'next/cache';
import { cookies } from 'next/headers';
import { redirect } from 'next/navigation';
import { APIServerError, formatApiDetail } from '@/lib/api-error';
import { parseJobId } from '@/lib/utils';
import type { FileListParams, JobListParams, PageInfo } from './types';

// Server-side API client - runs only on Next.js server (pages, server actions,
// route handlers). Can access Kubernetes service names like 'dashboard-backend-svc:8000'.
// Implements ServerApi (./types). The static demo build swaps in ./demo/server.ts.

export const API_BASE_URL = process.env.API_URL || 'http://localhost:8000';

export { APIServerError };

/**
 * Validate and convert job ID from string to number
 * @param jobId - Job ID as string (from URL params)
 * @returns Validated job ID as number
 * @throws APIServerError if job ID is invalid
 */
function validateJobId(jobId: string): number {
  const id = parseJobId(jobId);
  if (id === null) {
    throw new APIServerError(`Invalid job ID: "${jobId}" must be a whole number from 1 to 2147483647`, 400);
  }
  return id;
}

/**
 * Backend request with the session's bearer token. A non-2xx response throws
 * an APIServerError carrying the backend's `detail` (a 401 included: callers
 * decide how to leave the expired session, see fetchAPI and backendAction).
 */
export async function backendFetch<T>(
  endpoint: string,
  options?: RequestInit
): Promise<T> {
  const url = `${API_BASE_URL}${endpoint}`;

  // Get auth token from cookies
  const token = (await cookies()).get('auth_token')?.value;

  let response: Response;
  try {
    response = await fetch(url, {
      ...options,
      headers: {
        // A FormData body sets its own multipart Content-Type (with the boundary)
        ...(!(options?.body instanceof FormData) && { 'Content-Type': 'application/json' }),
        ...(token && { 'Authorization': `Bearer ${token}` }),
        ...options?.headers,
      },
    });
  } catch (error) {
    throw new APIServerError(error instanceof Error ? error.message : 'Network error');
  }

  if (!response.ok) {
    let errorMessage = `API Error: ${response.status} ${response.statusText}`;

    try {
      const errorData = await response.json();
      if (errorData.detail) {
        errorMessage = formatApiDetail(errorData.detail);
      } else if (typeof errorData.message === 'string') {
        errorMessage = errorData.message;
      }
    } catch {
      // If JSON parsing fails, use the default error message
    }

    throw new APIServerError(errorMessage, response.status);
  }

  return response.json();
}

// For server components: an expired/invalid token redirects to /logout, which
// clears the cookie (else the middleware keeps sending us here)
async function fetchAPI<T>(
  endpoint: string,
  options?: RequestInit
): Promise<T> {
  try {
    return await backendFetch<T>(endpoint, options);
  } catch (error) {
    if (error instanceof APIServerError && error.status === 401) {
      redirect('/logout');
    }
    throw error;
  }
}

export type ActionResult<T> =
  | { success: true; data: T }
  | { success: false; error: string };

/**
 * The failure a server action returns for an id that is not a positive
 * integer, or null for a valid one. Server actions can be called with any
 * arguments, and an id goes into the backend path: "1/../.." would reach
 * outside /api, which the /api proxy refuses (lib/proxy/rules.ts).
 */
export function invalidId(id: unknown, what = 'ID'): ActionResult<never> | null {
  return typeof id === 'number' && Number.isSafeInteger(id) && id > 0
    ? null
    : { success: false, error: `Invalid ${what}` };
}

/**
 * One backend mutation for a server action: on success the given pages are
 * revalidated. A failure becomes { success: false, error } for the UI, except
 * a 401, which redirects the client to /logout like the server pages do.
 */
export async function backendAction<T = unknown>(
  endpoint: string,
  options: RequestInit,
  { fallbackError, revalidate }: { fallbackError: string; revalidate: string[] }
): Promise<ActionResult<T>> {
  let data: T;
  try {
    data = await backendFetch<T>(endpoint, options);
  } catch (error) {
    if (error instanceof APIServerError && error.status === 401) {
      redirect('/logout');
    }
    console.error(`Server Action - ${fallbackError}:`, error);
    return { success: false, error: (error instanceof Error && error.message) || fallbackError };
  }

  for (const path of revalidate) {
    revalidatePath(path);
  }
  return { success: true, data };
}

/**
 * A paginated backend list: empty filters dropped (FastAPI rejects '' for
 * enums), the frontend's `size` sent as the backend's `page_size`, and the
 * page count derived where the endpoint doesn't return it.
 */
async function paginatedQuery<K extends string, T>(
  endpoint: string,
  key: K,
  params: Record<string, string | number | undefined>
): Promise<PageInfo & Record<K, T[]>> {
  const query = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) {
    if (value === undefined || value === '') continue;
    query.set(name === 'size' ? 'page_size' : name, String(value));
  }
  const qs = query.toString();
  const response = await fetchAPI<{ total: number; page: number; page_size: number; pages?: number } & Record<K, T[]>>(
    `${endpoint}${qs ? `?${qs}` : ''}`
  );
  return {
    ...response,
    size: response.page_size,
    pages: response.pages ?? (Math.ceil(response.total / response.page_size) || 1),
  };
}

// Jobs
export function getJobs(params: JobListParams = {}) {
  return paginatedQuery<'jobs', Job>('/api/jobs', 'jobs', { ...params });
}

export async function getJob(jobId: string): Promise<Job> {
  const id = validateJobId(jobId);
  return fetchAPI<Job>(`/api/jobs/${id}`);
}

export async function getJobProgress(jobId: string): Promise<JobProgress> {
  const id = validateJobId(jobId);
  return fetchAPI<JobProgress>(`/api/jobs/${id}/progress`);
}

export async function getJobPackages(jobId: string): Promise<JobPackage[]> {
  const id = validateJobId(jobId);
  return fetchAPI<JobPackage[]>(`/api/jobs/${id}/packages`);
}

// Files (paginated with search/sort)
export function getFiles(params: FileListParams = {}) {
  return paginatedQuery<'files', InputFileInfo>('/api/files', 'files', { ...params });
}

// Data access: server components read
// through this module. Mutations are server actions (actions/*) built on
// backendAction, except job submission (a multipart upload with progress, so
// it goes through the /api proxy from lib/api-client). Client components read
// what changes under them (comparisons, metrics, animation pages, health)
// through lib/api-client and react-query.
