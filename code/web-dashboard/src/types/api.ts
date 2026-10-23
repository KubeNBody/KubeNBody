// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// API Response Types

// Type aliases for commonly used types
export type JobType = 'distributed' | 'monolithic' | 'distributed_v2';
export type ForceAlgorithm = 'direct' | 'barnes_hut';
// How bodies are permuted once at V2 job creation so contiguous packages take a
// given shape: file order, seeded shuffle, Morton/Z-order spatial locality, or
// orthogonal recursive bisection (one axis-aligned box per package).
export type PartitionStrategy = 'linear' | 'random' | 'spatial' | 'orb';
export type IntegrationMethod =
  // Base integrators (selectable for any job type)
  | 'simple_euler' | 'symplectic_euler' | 'velocity_verlet' | 'rk4' | 'forest_ruth'
  // Distributed "predictor family": base integrator × background-predictor rung.
  // Composed by the submit form for distributed jobs; see lib/format-integration.
  | 'gvv_frozen' | 'gvv_linear' | 'gvv_accel'
  | 'grk4_frozen' | 'grk4_linear' | 'grk4_accel'
  | 'gfr_frozen' | 'gfr_linear' | 'gfr_accel';

export interface Job {
  job_id: number;
  status: JobStatus;
  num_epochs: number;
  current_epoch: number;
  package_size: number;
  num_packages: number;
  input_file_key: string;
  results_file_key?: string;
  progress_percentage: number;
  time_step: number;
  snapshot_interval: number;
  job_type: JobType;
  created_at: string;
  updated_at: string;
  // Execution record (migrations 020/021)
  started_at?: string | null;
  completed_at?: string | null;
  retry_count?: number | null;
  recovery_count?: number | null;
  lease_owner?: string | null;
  filename?: string;
  file_size?: number;
  gpu_enabled?: boolean;
  // Algorithm selection
  force_algorithm?: ForceAlgorithm;
  barnes_hut_theta?: number;
  softening_sq?: number;
  // Integration method
  integration_method?: IntegrationMethod;
  // Adaptive timestepping
  adaptive_enabled?: boolean;
  current_simulation_time?: number;
  // User-supplied free-text note
  description?: string | null;
  // User-supplied short title, shown instead of the file name
  name?: string | null;
  // Package partitioning (distributed_v2): 'linear' | 'random' | 'spatial' | 'orb'
  partition_strategy?: PartitionStrategy;
  partition_seed?: number;
}

export interface SelectableJob {
  job_id: number;
  status: string;
  job_type: string;
  filename: string | null;
  num_epochs: number;
  time_step: number;
  gpu_enabled: boolean;
  force_algorithm: string;
  integration_method: string;
  softening_sq?: number;
  description?: string | null;
  name?: string | null;
  created_at: string;
}

export interface JobCreate {
  num_epochs: number;
  package_size?: number;
  time_step: number;
  snapshot_interval: number;
  job_type: JobType;
  use_gpu: boolean;
  // Algorithm selection for monolithic and distributed_v2 jobs
  force_algorithm?: ForceAlgorithm;
  barnes_hut_theta?: number;
  softening_sq?: number;
  // Integration method
  integration_method?: IntegrationMethod;
  // Adaptive timestepping
  adaptive_enabled?: boolean;
  adaptive_eta?: number;
  adaptive_dt_min?: number;
  adaptive_dt_max?: number;
  // User-supplied free-text note
  job_description?: string;
  // User-supplied short title
  job_name?: string;
}

export interface JobProgress {
  job_id: number;
  status: JobStatus;
  current_epoch: number;
  total_epochs: number;
  packages_completed: number;
  total_packages: number;
  progress_percentage: number;
}

// V1 distributed work package (GET /api/jobs/{id}/packages, src/enums.py PackageStatus)
export type PackageStatus = 'PENDING' | 'IN_PROGRESS' | 'TO_MERGE' | 'MERGED' | 'FAILED';

export interface JobPackage {
  package_id: number;
  package_number: number;
  epoch_num: number;
  status: PackageStatus;
  file_key: string | null;
}

export interface SystemHealth {
  status: string;
  services: Record<string, unknown>;
  timestamp: string;
}

// Enums
// src/enums.py JobStatus (a string union like every other domain type here)
export const JOB_STATUSES = ['PENDING', 'IN_PROGRESS', 'PAUSED', 'COMPLETED', 'FAILED', 'CANCELLED'] as const;
export type JobStatus = (typeof JOB_STATUSES)[number];

// Input File Library Types
export interface InputFileInfo {
  file_id: number;
  user_friendly_name: string;
  original_filename: string;
  num_bodies: number;
  file_size: number;
  created_at: string;
  last_used_at: string;
  usage_count: number;
  description: string | null;
}

export interface InputFileListResponse {
  files: InputFileInfo[];
  total: number;
  page: number;
  page_size: number;
}

// 409 of a job submission whose new file name is taken (routes/jobs.py, v2/api_v2.py)
export interface FileUploadConflictResponse {
  error: 'name_conflict';
  message: string;
  existing_file: Pick<InputFileInfo, 'file_id' | 'user_friendly_name' | 'num_bodies' | 'file_size' | 'created_at' | 'usage_count'>;
}

// Animation Data Types
export interface AnimationSnapshot {
  epoch: number;
  particles: number[][]; // Array of [x, y, z, mass]
  center_of_mass: [number, number, number]; // [x, y, z] center of mass
}

// ============ V2 API Types (Redis-Centric Architecture) ============

export interface JobV2Create {
  num_epochs: number;
  time_step: number;
  package_size: number;
  snapshot_interval: number;
  // GPU
  gpu_enabled?: boolean;
  // Algorithm selection
  force_algorithm?: ForceAlgorithm;
  barnes_hut_theta?: number;
  softening_sq?: number;
  // Integration method
  integration_method?: IntegrationMethod;
  // Package partitioning (permute-once): 'linear' | 'random' | 'spatial' | 'orb'
  partition_strategy?: PartitionStrategy;
  partition_seed?: number;
  // Adaptive timestepping
  adaptive_enabled?: boolean;
  adaptive_eta?: number;
  adaptive_dt_min?: number;
  adaptive_dt_max?: number;
  // User-supplied free-text note
  job_description?: string;
  // User-supplied short title
  job_name?: string;
}

export interface JobV2CreateResponse {
  job_id: number;
  message: string;
  file_key: string;
  num_bodies: number;
}

// ============ Simulation Comparison Types ============

export interface EpochMetrics {
  epoch: number;
  simulation_time: number;
  kinetic_energy: number;
  potential_energy: number;
  total_energy: number;
  relative_energy_error: number; // (E - E0) / |E0|
  momentum: [number, number, number]; // [Px, Py, Pz]
  momentum_magnitude: number;
  angular_momentum: [number, number, number]; // [Lx, Ly, Lz]
  angular_momentum_magnitude: number;
}

export interface JobMetricsResponse {
  job_id: number;
  epochs: number[];
  simulation_times: number[];
  metrics: EpochMetrics[];
  energy_drift_percent: number;
  momentum_conserved: boolean;
  angular_momentum_conserved: boolean;
  computation_time_ms: number;
  /** false: an adaptive job's simulation times were not all recorded (the missing ones are estimated from the recorded neighbours) */
  simulation_times_exact?: boolean;
}

export interface JobComparisonSummary {
  job_id: number;
  status: string;
  job_type: string;
  num_epochs: number;
  current_epoch: number;
  snapshot_interval: number;
  force_algorithm: string;
  barnes_hut_theta?: number;
  softening_sq?: number;
  integration_method: string;
  time_step: number;
  gpu_enabled: boolean;
  filename?: string;
  description?: string | null;
  name?: string | null;
}

// ============ Job-to-Job Comparison Types ============

export interface SingleJobComparisonResult {
  job_id: number;
  job_info: JobComparisonSummary;

  // Trajectory divergence summary
  epochs_compared: number;
  position_rmse_final: number;
  velocity_rmse_final: number;

  // Energy comparison
  energy_drift_reference: number;
  energy_drift_comparison: number;
  /** E0 of each job's drift (absent in results stored before it was recorded) */
  energy_initial_reference?: number | null;
  energy_initial_comparison?: number | null;

  // Momentum comparison
  momentum_drift_reference: number;
  momentum_drift_comparison: number;

  // Timeseries for charts
  epochs: number[];
  simulation_times: number[];
  position_rmse_timeseries: number[];
  velocity_rmse_timeseries: number[];
  energy_reference_timeseries: number[];
  energy_comparison_timeseries: number[];
  momentum_reference_timeseries: number[];
  momentum_comparison_timeseries: number[];
}

export interface SkippedJobInfo {
  job_id: number;
  reason: string;
}

// Persistent comparison jobs
export interface ComparisonJobResponse {
  comparison_id: number;
  status: 'PENDING' | 'COMPUTING' | 'COMPLETED' | 'COMPLETED_EMPTY' | 'FAILED';
  reference_job_id: number;
  comparison_job_ids: number[];
  max_snapshots: number | null; // null = all snapshots
  created_at: string;
  updated_at: string;
  started_at?: string;
  computation_time_ms?: number;
  jobs_compared: number;
  jobs_skipped: number;
  error_message?: string;
  stale: boolean;
  stale_reason?: string;
  results?: JobToJobComparisonResponse;
}

export interface ComparisonJobListResponse {
  comparisons: ComparisonJobResponse[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface JobToJobComparisonResponse {
  reference_job_id: number;
  reference_job_info: JobComparisonSummary;
  comparisons: SingleJobComparisonResult[];
  skipped_jobs: SkippedJobInfo[];
  computation_time_ms: number;
}

// GET /api/jobs/{id}/animation-data/paginated: one page of snapshots
export interface PaginatedAnimationDataResponse {
  job_id: number;
  snapshot_interval: number;
  total_epochs: number;
  total_snapshots: number;
  offset: number;
  limit: number;
  has_next: boolean;
  snapshots: AnimationSnapshot[];
}
