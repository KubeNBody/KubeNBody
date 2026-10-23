// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// The data layer's interfaces. Each has two implementations in src/:
//
//   real (normal build)            demo (static export)
//   lib/data/server.ts             lib/data/demo/server.ts
//   actions/{jobs,files,auth}.ts   lib/data/demo/actions/{jobs,files,auth}.ts
//
// Pages import the real modules. In the demo build next.config.js swaps those
// imports for the demo ones. The checks at the bottom are type-only, so
// `npm run type-check` (and `next build`) fails as soon as either side drifts
// from an interface, in whichever build is being checked.
import type { APIServerError } from '@/lib/api-error';
import type { InputFileInfo, Job, JobPackage, JobProgress } from '@/types/api';

export interface JobListParams {
  page?: number;
  size?: number;
  status?: string;
  search?: string;
  job_type?: string;
  integration_method?: string;
}

export interface FileListParams {
  page?: number;
  size?: number;
  search?: string;
  sort_by?: string;
}

export interface PageInfo {
  total: number;
  page: number;
  size: number;
  pages: number;
}

/** Reads made by the server components */
export interface ServerApi {
  APIServerError: typeof APIServerError;
  getJobs(params?: JobListParams): Promise<PageInfo & { jobs: Job[] }>;
  getJob(jobId: string): Promise<Job>;
  getJobProgress(jobId: string): Promise<JobProgress>;
  getJobPackages(jobId: string): Promise<JobPackage[]>;
  getFiles(params?: FileListParams): Promise<PageInfo & { files: InputFileInfo[] }>;
}

/** What a mutation reports to the UI (an expired session redirects instead) */
export type MutationResult = { success: true; data: unknown } | { success: false; error: string };

export interface JobActions {
  cancelJob(jobId: number, jobType?: string): Promise<MutationResult>;
  pauseJob(jobId: number, jobType?: string): Promise<MutationResult>;
  resumeJob(jobId: number, jobType?: string): Promise<MutationResult>;
  deleteJob(jobId: number): Promise<MutationResult>;
  recoverJob(jobId: number, force?: boolean): Promise<MutationResult>;
  updateJobDescription(jobId: number, description: string | null): Promise<MutationResult>;
  updateJobName(jobId: number, name: string | null): Promise<MutationResult>;
}

export interface FileActions {
  updateFile(fileId: number, name: string, description: string): Promise<MutationResult>;
  deleteFile(fileId: number): Promise<MutationResult>;
}

/** What the login form shows after an attempt (a success navigates away instead) */
export type LoginState = { error: string } | undefined;

export interface AuthActions {
  /** A form action (useActionState): navigates to the dashboard on success, else returns the error */
  login(previous: LoginState, form: FormData): Promise<LoginState>;
  logout(): Promise<void>;
}

// --- implementation checks (type-only: nothing is imported at runtime) ---
type Implements<Module extends Contract, Contract> = Module;

export type CheckServer = Implements<typeof import('./server'), ServerApi>;
export type CheckJobActions = Implements<typeof import('@/actions/jobs'), JobActions>;
export type CheckFileActions = Implements<typeof import('@/actions/files'), FileActions>;
export type CheckAuthActions = Implements<typeof import('@/actions/auth'), AuthActions>;

export type CheckDemoServer = Implements<typeof import('./demo/server'), ServerApi>;
export type CheckDemoJobActions = Implements<typeof import('./demo/actions/jobs'), JobActions>;
export type CheckDemoFileActions = Implements<typeof import('./demo/actions/files'), FileActions>;
export type CheckDemoAuthActions = Implements<typeof import('./demo/actions/auth'), AuthActions>;
