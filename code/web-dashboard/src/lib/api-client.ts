// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import axios, { AxiosInstance, AxiosError, AxiosResponse } from 'axios';
import {
  AnimationSnapshot,
  ComparisonJobListResponse,
  ComparisonJobResponse,
  InputFileListResponse,
  Job,
  JobComparisonSummary,
  JobCreate,
  JobMetricsResponse,
  JobToJobComparisonResponse,
  JobV2Create,
  JobV2CreateResponse,
  PaginatedAnimationDataResponse,
  SelectableJob,
  SystemHealth,
} from '@/types/api';
import { IS_DEMO } from './demo';
import { formatApiDetail } from './api-error';
import { toFormData, type FormField } from './form-data';

/** Where a submitted job's input file comes from: an upload, or a library file */
export interface JobFileSource {
  file?: File;
  fileId?: number;
  /** Library name and description of an uploaded file */
  userFriendlyName?: string;
  description?: string;
  /** Upload even when the library name is taken (else a 409 name_conflict) */
  forceNew?: boolean;
  onProgress?: (percent: number) => void;
}


// Count the lines with any non-whitespace byte (what split('\n') + trim filter
// would count) by streaming the file instead of decoding it into one string
async function countNonEmptyLines(file: File): Promise<number> {
  const reader = file.stream().getReader();
  let count = 0;
  let lineHasContent = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    for (let i = 0; i < value.length; i++) {
      const byte = value[i];
      if (byte === 10) { // \n
        if (lineHasContent) count++;
        lineHasContent = false;
      } else if (byte !== 32 && byte !== 9 && byte !== 13) { // not space, tab, \r
        lineHasContent = true;
      }
    }
  }
  if (lineHasContent) count++;
  return count;
}

/** True for a request cancelled through an AbortSignal (axios wraps it in APIError) */
export function isAbortError(error: unknown): boolean {
  const e = error as { code?: string; name?: string } | null;
  return !!e && (e.code === 'ERR_CANCELED' || e.name === 'AbortError' || e.name === 'CanceledError');
}

export class APIError extends Error {
  constructor(
    message: string,
    public status?: number,
    public code?: string,
    public response?: AxiosResponse
  ) {
    super(message);
    this.name = 'APIError';
  }
}

export class APIClient {
  private client: AxiosInstance;

  constructor() {
    // Relative URLs (/api/...): the Next.js proxy route forwards them to the
    // backend with the httpOnly auth cookie turned into a bearer token
    this.client = axios.create({
      timeout: 30000,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Static demo build: answer requests from bundled fixtures instead of the network
    if (IS_DEMO) {
      this.client.defaults.adapter = (config) =>
        import('./demo/adapter').then(({ demoAdapter }) => demoAdapter(config));
    }

    // Response interceptor
    this.client.interceptors.response.use(
      (response) => response,
      (error: AxiosError) => {
        const apiError = new APIError(
          error.message || 'Network error',
          error.response?.status,
          error.code,
          error.response // Preserve entire response object for conflict handling
        );

        if (error.response?.data && typeof error.response.data === 'object') {
          const data = error.response.data as any;
          if (data.detail) {
            apiError.message = formatApiDetail(data.detail);
          } else if (typeof data.message === 'string') {
            apiError.message = data.message;
          }
        }

        // Handle 401 Unauthorized - clear the (httpOnly) cookie via /logout, which
        // redirects to login. Going to /login directly would bounce back to /dashboard
        if (error.response?.status === 401) {
          console.error('Authentication error - logging out');
          if (typeof window !== 'undefined') {
            window.location.href = '/logout';
          }
        }

        // A cancelled request (e.g. a superseded seek) is not an error worth logging
        if (!axios.isCancel(error)) {
          console.error('API Error:', apiError);
        }
        return Promise.reject(apiError);
      }
    );
  }

  // Health & System
  async getHealth(): Promise<SystemHealth> {
    const response = await this.client.get('/api/health');
    return response.data;
  }

  // Jobs
  async getSelectableJobs(status?: string): Promise<SelectableJob[]> {
    const params = status ? { status } : {};
    const response = await this.client.get('/api/jobs/selectable', { params });
    return response.data;
  }


  /**
   * Submit a job (POST /api/jobs for monolithic / distributed v1, /api/v2/jobs for
   * v2): the request fields plus either an uploaded file (with its library name
   * and description) or a library file id. No timeout: a large upload can take
   * longer than the default, and aborting it client-side doesn't stop the backend
   * from creating the job.
   */
  private async submitJob<T>(
    endpoint: '/api/jobs' | '/api/v2/jobs',
    fields: Record<string, FormField>,
    source: JobFileSource
  ): Promise<T> {
    const form = toFormData({
      ...fields,
      // The input file, from the source only (JobCreate / JobV2Create have no file fields)
      file: source.file,
      file_id: source.fileId,
      user_friendly_name: source.userFriendlyName,
      description: source.description,
      force_new: source.file ? (source.forceNew ?? false) : undefined,
    });
    const response = await this.client.post(endpoint, form, {
      timeout: 0,
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: (event) => {
        if (event.total && source.onProgress) source.onProgress(Math.round((event.loaded * 100) / event.total));
      },
    });
    return response.data;
  }

  createJob(jobData: JobCreate, source: JobFileSource): Promise<Job> {
    return this.submitJob<Job>('/api/jobs', { ...jobData }, source);
  }

  createV2Job(jobData: JobV2Create, source: JobFileSource): Promise<JobV2CreateResponse> {
    return this.submitJob<JobV2CreateResponse>('/api/v2/jobs', { ...jobData }, source);
  }

  // Input Files
  async getInputFiles(): Promise<InputFileListResponse> {
    const response = await this.client.get('/api/files/input');
    return response.data;
  }

  // File validation helper - supports CSV and space-separated formats.
  // Checks the header and first rows from the start of the file only, and counts
  // rows by streaming the bytes, so a large file is never held as one string.
  async validateCSVFile(file: File): Promise<{ valid: boolean; error?: string; preview?: any }> {
    // Accept common text file extensions
    const validExtensions = ['.csv', '.txt', '.dat'];
    const hasValidExtension = validExtensions.some(ext =>
      file.name.toLowerCase().endsWith(ext)
    );

    if (!hasValidExtension) {
      return {
        valid: false,
        error: 'File must be a text file (CSV, TXT, or DAT)'
      };
    }

    if (file.size > 100 * 1024 * 1024) { // 100MB
      return { valid: false, error: 'File size must be less than 100MB' };
    }

    let lines: string[];
    let rowCount: number;
    try {
      const HEAD_BYTES = 64 * 1024;
      let head = await file.slice(0, HEAD_BYTES).text();
      // Drop the last line if the slice cut it off mid-way
      if (file.size > HEAD_BYTES) {
        head = head.slice(0, head.lastIndexOf('\n') + 1);
      }
      lines = head.split('\n').filter(line => line.trim().length > 0);
      rowCount = await countNonEmptyLines(file);
    } catch {
      return { valid: false, error: 'Failed to read file' };
    }

    try {
      if (lines.length < 1) {
        return { valid: false, error: 'File must contain at least one data row' };
      }

      const firstLine = lines[0].trim();

      // Detect format: CSV (with commas) or space-separated
      const isCSV = firstLine.includes(',');

      if (isCSV) {
        // Validate CSV format
        const header = lines[0].split(',').map(h => h.trim().toLowerCase());
        const requiredColumns = ['n', 'idbody', 'm', 'x', 'y', 'z', 'vx', 'vy', 'vz'];

        if (lines.length < 2) {
          return { valid: false, error: 'CSV must contain at least a header and one data row' };
        }

        const missingColumns = requiredColumns.filter(col => !header.includes(col));
        if (missingColumns.length > 0) {
          return {
            valid: false,
            error: `Missing required columns: ${missingColumns.join(', ')}`
          };
        }

        // Parse and validate sample rows for preview
        const sampleRows = lines.slice(1, 6).map((line, idx) => {
          const values = line.split(',');
          const row = requiredColumns.reduce((row, col) => {
            const colIndex = header.indexOf(col);
            row[col] = values[colIndex]?.trim();
            return row;
          }, {} as any);

          // Validate mass is positive in sample rows
          const mass = parseFloat(row.m);
          if (!isNaN(mass) && mass <= 0) {
            throw new Error(`Row ${idx + 2}: Mass must be positive, got ${mass}`);
          }

          return row;
        });

        // Client-side checks cover the header and the first five data rows only
        return {
          valid: true,
          preview: {
            format: 'CSV',
            totalRows: rowCount - 1,
            sampleData: sampleRows,
            fileSize: file.size,
            fileName: file.name,
            note: 'File structure validated'
          }
        };
      }

      // Validate space-separated format
      const values = firstLine.split(/\s+/);

      if (values.length !== 7) {
        return {
          valid: false,
          error: `Space-separated format must have 7 columns (m, x, y, z, vx, vy, vz). Found ${values.length} columns.`
        };
      }

      // Strict number validation (matches Python float() behavior)
      try {
        for (let i = 0; i < values.length; i++) {
          const v = values[i].trim();
          const num = parseFloat(v);

          // Check if parsing failed
          if (isNaN(num)) {
            throw new Error(`Invalid number at position ${i + 1}: "${v}"`);
          }

          // Check for infinity
          if (!isFinite(num)) {
            throw new Error(`Number out of range at position ${i + 1}: "${v}"`);
          }

          // Strict check: reject trailing garbage like "123abc"
          // Match Python's strict float() behavior
          const cleanNum = v.replace(/\s+/g, '');
          if (!/^[+-]?(\d+\.?\d*|\d*\.\d+)([eE][+-]?\d+)?$/.test(cleanNum)) {
            throw new Error(`Invalid number format at position ${i + 1}: "${v}"`);
          }

          // Check mass is positive (first column)
          if (i === 0 && num <= 0) {
            throw new Error(`Mass (first column) must be positive, got ${num}`);
          }
        }
      } catch (error) {
        return {
          valid: false,
          error: error instanceof Error ? error.message : 'Space-separated format must contain valid numeric values'
        };
      }

      // Parse sample rows for preview
      const sampleRows = lines.slice(0, 5).map((line, index) => {
        const vals = line.trim().split(/\s+/);
        return {
          n: (index + 1).toString(),
          idbody: (index + 1).toString(),
          m: vals[0],
          x: vals[1],
          y: vals[2],
          z: vals[3],
          vx: vals[4],
          vy: vals[5],
          vz: vals[6]
        };
      });

      // Client-side checks cover the first line only
      return {
        valid: true,
        preview: {
          format: 'Space-separated',
          totalRows: rowCount,
          sampleData: sampleRows,
          fileSize: file.size,
          fileName: file.name,
          note: 'File structure validated\nn and idBody will be auto-generated from line numbers'
        }
      };
    } catch {
      return { valid: false, error: 'Failed to parse file' };
    }
  }

  async getJobAnimationDataPaginated(
    jobId: number,
    offset: number = 0,
    limit: number = 20,
    signal?: AbortSignal
  ): Promise<PaginatedAnimationDataResponse> {
    const response = await this.client.get<PaginatedAnimationDataResponse>(
      `/api/jobs/${jobId}/animation-data/paginated`,
      {
        params: { offset, limit },
        signal,
      }
    );
    return response.data;
  }

  // ============ Comparison API Methods ============

  async getJobMetrics(
    jobId: number,
    options?: { maxSnapshots?: number; forceRefresh?: boolean }
  ): Promise<JobMetricsResponse> {
    const params = new URLSearchParams();
    if (options?.maxSnapshots) params.append('max_snapshots', options.maxSnapshots.toString());
    if (options?.forceRefresh) params.append('force_refresh', 'true');

    // First-ever computation for a large job streams + analyzes every
    // snapshot server-side and can take minutes (cached afterwards) —
    // the 30s default timeout aborts it and surfaces as a failed fetch.
    const response = await this.client.get(
      `/api/comparison/jobs/${jobId}/metrics${params.toString() ? '?' + params.toString() : ''}`,
      { timeout: 300000 }
    );
    return response.data;
  }

  async getMultipleJobSummaries(jobIds: number[]): Promise<JobComparisonSummary[]> {
    const response = await this.client.get(`/api/comparison/jobs/summaries?job_ids=${jobIds.join(',')}`);
    return response.data;
  }

  async compareJobs(
    referenceJobId: number,
    comparisonJobIds: number[],
    options?: { maxSnapshots?: number }
  ): Promise<JobToJobComparisonResponse> {
    const response = await this.client.post('/api/comparison/compare', {
      reference_job_id: referenceJobId,
      comparison_job_ids: comparisonJobIds,
      max_snapshots: options?.maxSnapshots,
    }, { timeout: 300000 });
    return response.data;
  }
  // ===== Persistent Comparison Jobs =====

  async createComparisonJob(
    referenceJobId: number,
    comparisonJobIds: number[],
    maxSnapshots?: number
  ): Promise<ComparisonJobResponse> {
    const response = await this.client.post('/api/comparison/saved', {
      reference_job_id: referenceJobId,
      comparison_job_ids: comparisonJobIds,
      max_snapshots: maxSnapshots,
    });
    return response.data;
  }

  async getComparisonJob(comparisonId: number): Promise<ComparisonJobResponse> {
    const response = await this.client.get(`/api/comparison/saved/${comparisonId}`);
    return response.data;
  }

  async listComparisonJobs(
    page: number = 1,
    pageSize: number = 20,
    status?: string
  ): Promise<ComparisonJobListResponse> {
    const params: Record<string, string | number> = { page, page_size: pageSize };
    if (status) params.status = status;
    const response = await this.client.get('/api/comparison/saved', { params });
    return response.data;
  }

  async deleteComparisonJob(comparisonId: number): Promise<void> {
    await this.client.delete(`/api/comparison/saved/${comparisonId}`);
  }
}

// Export singleton instance
export const apiClient = new APIClient();