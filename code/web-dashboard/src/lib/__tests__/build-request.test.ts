// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import { buildV1Request, buildV2Request, finalIntegrationMethod } from '@/components/submit/build-request';
import { jobSchema, type JobFormData } from '@/components/submit/job-form';

const form = (overrides: Partial<JobFormData> = {}): JobFormData =>
  jobSchema.parse({ num_epochs: 1000, time_step: 0.001, snapshot_interval: 10, job_type: 'distributed_v2', ...overrides });

describe('finalIntegrationMethod', () => {
  it('folds the predictor rung into distributed multi-stage methods', () => {
    expect(finalIntegrationMethod(form({ integration_method: 'rk4', background_predictor: 'accel' }))).toBe('grk4_accel');
    expect(finalIntegrationMethod(form({ job_type: 'distributed', integration_method: 'velocity_verlet', background_predictor: 'frozen' }))).toBe('gvv_frozen');
  });

  it('submits the base method for monolithic jobs and Euler methods', () => {
    expect(finalIntegrationMethod(form({ job_type: 'monolithic', integration_method: 'rk4', background_predictor: 'accel' }))).toBe('rk4');
    expect(finalIntegrationMethod(form({ integration_method: 'symplectic_euler' }))).toBe('symplectic_euler');
  });
});

describe('buildV2Request', () => {
  it('defaults the package size and drops the seed unless the shuffle is random', () => {
    const r = buildV2Request(form({ partition_strategy: 'orb', partition_seed: 7 }));
    expect(r.package_size).toBe(1000);
    expect(r.partition_strategy).toBe('orb');
    expect(r.partition_seed).toBeUndefined();
    expect(buildV2Request(form({ partition_strategy: 'random', partition_seed: 7 })).partition_seed).toBe(7);
  });

  it('sends the adaptive parameters only with adaptive stepping on, GPU jobs included', () => {
    const r = buildV2Request(form({ use_gpu: true, adaptive_enabled: true, adaptive_eta: 0.05 }));
    expect(r.adaptive_enabled).toBe(true);
    expect(r.adaptive_eta).toBe(0.05);
    expect(buildV2Request(form({ adaptive_enabled: false, adaptive_eta: 0.05 })).adaptive_eta).toBeUndefined();
  });

  it('trims the description and leaves an empty one out', () => {
    expect(buildV2Request(form({ job_description: '  run A  ' })).job_description).toBe('run A');
    expect(buildV2Request(form({ job_description: '   ' })).job_description).toBeUndefined();
  });
});

describe('buildV1Request', () => {
  it('trims the job name and leaves an empty one out, on both endpoints', () => {
    expect(buildV1Request(form({ job_name: '  Figure eight RK4 ' })).job_name).toBe('Figure eight RK4');
    expect(buildV2Request(form({ job_name: '  ' })).job_name).toBeUndefined();
  });

  it('sends no package size for monolithic jobs', () => {
    expect(buildV1Request(form({ job_type: 'monolithic', package_size: 50 })).package_size).toBeUndefined();
    expect(buildV1Request(form({ job_type: 'distributed' })).package_size).toBe(100);
    expect(buildV1Request(form({ job_type: 'distributed', package_size: 25 })).package_size).toBe(25);
  });
});
