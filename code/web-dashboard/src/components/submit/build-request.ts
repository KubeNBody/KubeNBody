// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// The submit form's values -> the backend's job request. Pure, so the rules the
// form encodes are unit-tested (lib/__tests__/build-request.test.ts).
import { composePredictorMethod, supportsPredictor } from '@/lib/format-integration';
import type { IntegrationMethod, JobCreate, JobV2Create } from '@/types/api';
import type { JobFormData } from './job-form';

export const DEFAULT_PACKAGE_SIZE = { distributed: 100, distributed_v2: 1000 } as const;

export const isDistributed = (jobType: JobFormData['job_type']) => jobType === 'distributed' || jobType === 'distributed_v2';

/**
 * The integration method to submit. For a distributed job running a multi-stage
 * integrator, the base method and the background-predictor rung fold into a
 * g-named method (rk4 + accel -> grk4_accel). Otherwise the base method as is.
 */
export function finalIntegrationMethod(data: Pick<JobFormData, 'job_type' | 'integration_method' | 'background_predictor'>): IntegrationMethod {
  return isDistributed(data.job_type) && supportsPredictor(data.integration_method)
    ? composePredictorMethod(data.integration_method, data.background_predictor)
    : data.integration_method;
}

/** The adaptive step's parameters are sent only with adaptive stepping on */
const adaptiveFields = (data: JobFormData) => {
  const on = data.adaptive_enabled;
  return {
    adaptive_enabled: on,
    adaptive_eta: on ? data.adaptive_eta : undefined,
    adaptive_dt_min: on ? data.adaptive_dt_min : undefined,
    adaptive_dt_max: on ? data.adaptive_dt_max : undefined,
  };
};

const description = (data: JobFormData) => data.job_description?.trim() || undefined;
const name = (data: JobFormData) => data.job_name?.trim() || undefined;

/** POST /api/jobs (monolithic and distributed v1) */
export function buildV1Request(data: JobFormData): JobCreate {
  return {
    num_epochs: data.num_epochs,
    package_size: data.job_type === 'monolithic' ? undefined : (data.package_size ?? DEFAULT_PACKAGE_SIZE.distributed),
    time_step: data.time_step,
    snapshot_interval: data.snapshot_interval,
    job_type: data.job_type,
    use_gpu: data.use_gpu,
    force_algorithm: data.force_algorithm,
    barnes_hut_theta: data.barnes_hut_theta,
    softening_sq: data.softening_sq,
    integration_method: finalIntegrationMethod(data),
    ...adaptiveFields(data),
    job_description: description(data),
    job_name: name(data),
  };
}

/** POST /api/v2/jobs */
export function buildV2Request(data: JobFormData): JobV2Create {
  return {
    num_epochs: data.num_epochs,
    time_step: data.time_step,
    package_size: data.package_size ?? DEFAULT_PACKAGE_SIZE.distributed_v2,
    snapshot_interval: data.snapshot_interval,
    gpu_enabled: data.use_gpu,
    force_algorithm: data.force_algorithm,
    barnes_hut_theta: data.barnes_hut_theta,
    softening_sq: data.softening_sq,
    integration_method: finalIntegrationMethod(data),
    partition_strategy: data.partition_strategy,
    // The seed only matters for the random shuffle
    partition_seed: data.partition_strategy === 'random' ? (data.partition_seed ?? 0) : undefined,
    ...adaptiveFields(data),
    job_description: description(data),
    job_name: name(data),
  };
}
