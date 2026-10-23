// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import { jobSchema, optionalNumber } from '@/components/submit/job-form';

const valid = {
  num_epochs: 1000,
  time_step: 0.001,
  snapshot_interval: 10,
  job_type: 'monolithic' as const,
};

describe('jobSchema', () => {
  it('accepts a minimal job and fills the defaults', () => {
    const parsed = jobSchema.parse(valid);
    expect(parsed).toMatchObject({
      use_gpu: false,
      force_algorithm: 'direct',
      integration_method: 'symplectic_euler',
      background_predictor: 'linear',
      partition_strategy: 'linear',
      adaptive_enabled: false,
    });
  });

  it('rejects out-of-range values', () => {
    expect(jobSchema.safeParse({ ...valid, num_epochs: 0 }).success).toBe(false);
    expect(jobSchema.safeParse({ ...valid, num_epochs: 1.5 }).success).toBe(false);
    expect(jobSchema.safeParse({ ...valid, time_step: 0 }).success).toBe(false);
    expect(jobSchema.safeParse({ ...valid, time_step: 2 }).success).toBe(false);
    expect(jobSchema.safeParse({ ...valid, job_description: 'x'.repeat(1001) }).success).toBe(false);
  });

  it('rejects Barnes–Hut on the GPU, which has direct summation only', () => {
    const gpuBh = jobSchema.safeParse({ ...valid, use_gpu: true, force_algorithm: 'barnes_hut' });
    expect(gpuBh.success).toBe(false);
    expect(gpuBh.success ? [] : gpuBh.error.issues.map((i) => i.path.join('.'))).toEqual(['force_algorithm']);
    expect(jobSchema.safeParse({ ...valid, use_gpu: true, force_algorithm: 'direct' }).success).toBe(true);
    expect(jobSchema.safeParse({ ...valid, force_algorithm: 'barnes_hut' }).success).toBe(true);
  });

  it('rejects the NaN a blank required number field produces', () => {
    expect(jobSchema.safeParse({ ...valid, time_step: NaN }).success).toBe(false);
  });
});

describe('optionalNumber', () => {
  it('maps blank input to undefined and numbers through', () => {
    expect(optionalNumber('')).toBeUndefined();
    expect(optionalNumber(null)).toBeUndefined();
    expect(optionalNumber('0.5')).toBe(0.5);
  });
});
