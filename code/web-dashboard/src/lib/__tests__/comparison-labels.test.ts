// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import { comparisonLabels, jobWithLabel } from '@/lib/domain/comparison-labels';

const base = { job_type: 'monolithic', force_algorithm: 'direct', integration_method: 'velocity_verlet', time_step: 0.001, softening_sq: 1, gpu_enabled: false };

describe('comparisonLabels', () => {
  it('names each job by what differs from the reference', () => {
    const l = comparisonLabels(base, [
      { ...base, job_id: 42, integration_method: 'simple_euler' },
      { ...base, job_id: 43, integration_method: 'rk4' },
    ]);
    expect(l.reference).toBe('Velocity Verlet');
    expect(l.jobs).toEqual({ 42: 'Simple Euler', 43: 'RK4' });
  });

  it('lists only the differing dimensions of each job, the reference gets all that vary', () => {
    const l = comparisonLabels(base, [
      { ...base, job_id: 53, force_algorithm: 'barnes_hut' },
      { ...base, job_id: 54, job_type: 'distributed_v2', integration_method: 'gvv_accel' },
    ]);
    expect(l.jobs[53]).toBe('Barnes-Hut');
    expect(l.jobs[54]).toBe('Distributed V2 · Velocity Verlet · accel');
    expect(l.reference).toBe('Monolithic · Direct · Velocity Verlet');
  });

  it('treats a missing force algorithm as direct and leaves identical setups unlabelled', () => {
    const l = comparisonLabels(base, [{ ...base, job_id: 7, force_algorithm: '' }]);
    expect(l.reference).toBe('');
    expect(l.jobs[7]).toBe('');
    expect(jobWithLabel(7, l.jobs[7])).toBe('#7');
  });
});
