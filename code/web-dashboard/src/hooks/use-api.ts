// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useMutation, useQuery, useQueryClient, UseQueryOptions } from '@tanstack/react-query';
import { apiClient, APIError, type JobFileSource } from '@/lib/api-client';
import {
  InputFileListResponse,
  JobComparisonSummary,
  JobCreate,
  JobMetricsResponse,
  JobToJobComparisonResponse,
  JobV2Create,
  SelectableJob,
  SystemHealth,
} from '@/types/api';

// Query keys
export const queryKeys = {
  health: ['health'],
  selectableJobs: (status?: string) => ['jobs', 'selectable', status],
  inputFiles: ['inputFiles'],
  // Comparison query keys
  jobMetrics: (id: number, maxSnapshots?: number) => ['comparison', 'metrics', id, maxSnapshots],
  multipleJobSummaries: (ids: number[]) => ['comparison', 'summaries', ids],
  // Persistent comparisons
  comparisonJobs: (page?: number, status?: string) => ['comparison', 'saved', 'list', { page, status }],
  comparisonJob: (id: number) => ['comparison', 'saved', 'detail', id],
} as const;

// Health & System hooks
export function useHealth(options?: Partial<UseQueryOptions<SystemHealth, APIError>>) {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: () => apiClient.getHealth(),
    staleTime: 30 * 1000, // 30 seconds
    refetchInterval: 30 * 1000, // keep the sidebar connection status current
    ...options,
  });
}

export function useSelectableJobs(
  status?: string,
  options?: Partial<UseQueryOptions<SelectableJob[], APIError>>
) {
  return useQuery({
    queryKey: queryKeys.selectableJobs(status),
    queryFn: () => apiClient.getSelectableJobs(status),
    staleTime: 30 * 1000,
    ...options,
  });
}

// Job submission (the jobs list is server-rendered: the job page we navigate to
// fetches fresh. Only the library's usage counts are cached here)
export function useCreateJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ jobData, ...source }: { jobData: JobCreate } & JobFileSource) => apiClient.createJob(jobData, source),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.inputFiles }),
  });
}

// File validation hook
export function useFileValidation() {
  return useMutation({
    mutationFn: (file: File) => apiClient.validateCSVFile(file),
  });
}

// Input Files hook
export function useInputFiles(options?: Partial<UseQueryOptions<InputFileListResponse, APIError>>) {
  return useQuery({
    queryKey: queryKeys.inputFiles,
    queryFn: () => apiClient.getInputFiles(),
    staleTime: 5 * 60 * 1000, // 5 minutes
    // The files page edits/deletes through server actions, which this cache never
    // hears about: refetch whenever the submit page mounts
    refetchOnMount: 'always',
    ...options,
  });
}

// ============ V2 API Hooks (Redis-Centric Architecture) ============

export function useCreateV2Job() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ jobData, ...source }: { jobData: JobV2Create } & JobFileSource) => apiClient.createV2Job(jobData, source),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.inputFiles }),
  });
}

// ============ Comparison Hooks ============

/**
 * Fetch conservation metrics for a single job.
 * Returns energy, momentum, angular momentum per epoch.
 */
export function useJobMetrics(
  jobId: number,
  options?: {
    maxSnapshots?: number;
    enabled?: boolean;
  }
) {
  const { maxSnapshots, enabled = true } = options || {};

  return useQuery<JobMetricsResponse, APIError>({
    queryKey: queryKeys.jobMetrics(jobId, maxSnapshots),
    queryFn: () => apiClient.getJobMetrics(jobId, { maxSnapshots }),
    staleTime: 5 * 60 * 1000, // 5 minutes - metrics don't change for completed jobs
    enabled: enabled && !!jobId,
  });
}

/**
 * Fetch summaries for multiple jobs at once (for comparison table).
 */
export function useMultipleJobSummaries(
  jobIds: number[],
  options?: Partial<UseQueryOptions<JobComparisonSummary[], APIError>>
) {
  return useQuery({
    queryKey: queryKeys.multipleJobSummaries(jobIds),
    queryFn: () => apiClient.getMultipleJobSummaries(jobIds),
    staleTime: 5 * 60 * 1000, // 5 minutes
    enabled: jobIds.length > 0,
    ...options,
  });
}

// ===== Persistent Comparison Job Hooks =====

export function useComparisonJobs(page: number = 1, status?: string) {
  return useQuery({
    queryKey: queryKeys.comparisonJobs(page, status),
    queryFn: () => apiClient.listComparisonJobs(page, 20, status),
    // Poll while any listed comparison is still being computed
    refetchInterval: (query) =>
      query.state.data?.comparisons.some(
        (c) => c.status === 'PENDING' || c.status === 'COMPUTING'
      )
        ? 3000
        : false,
  });
}

export function useComparisonJob(comparisonId: number | null) {
  return useQuery({
    queryKey: queryKeys.comparisonJob(comparisonId ?? 0),
    queryFn: () => apiClient.getComparisonJob(comparisonId as number),
    enabled: !!comparisonId,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (data && (data.status === 'PENDING' || data.status === 'COMPUTING')) {
        return 2000; // Poll every 2s while computing
      }
      return false;
    },
  });
}

export function useCreateComparisonJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (params: { referenceJobId: number; comparisonJobIds: number[]; maxSnapshots?: number }) =>
      apiClient.createComparisonJob(params.referenceJobId, params.comparisonJobIds, params.maxSnapshots),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['comparison', 'saved'] });
    },
  });
}

export function useDeleteComparisonJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (comparisonId: number) => apiClient.deleteComparisonJob(comparisonId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['comparison', 'saved'] });
    },
  });
}