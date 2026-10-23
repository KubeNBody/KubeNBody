// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import { referenceSeries, relativeDrift } from '@/components/comparison/series';
import type { SingleJobComparisonResult } from '@/types/api';

const result = (job_id: number, times: number[], energy: number[], e0?: number): SingleJobComparisonResult =>
  ({
    job_id,
    simulation_times: times,
    energy_reference_timeseries: energy,
    energy_comparison_timeseries: energy,
    energy_initial_reference: e0,
    energy_initial_comparison: e0,
  }) as unknown as SingleJobComparisonResult;

describe('relativeDrift', () => {
  it('measures against E0 at t = 0, not the first sampled snapshot', () => {
    expect(relativeDrift([-0.9, -0.8], -1)).toEqual([expect.closeTo(0.1, 12), expect.closeTo(0.2, 12)]);
  });

  it('falls back to the first sample for results stored without E0', () => {
    expect(relativeDrift([-1, -0.5], undefined)).toEqual([0, 0.5]);
    expect(relativeDrift([-1, -0.5], null)).toEqual([0, 0.5]);
  });
});

describe('referenceSeries', () => {
  it('draws the reference from the comparison that samples it most', () => {
    const sparse = result(2, [1], [-1.1]);
    const dense = result(3, [1, 2, 3], [-1.1, -1.2, -1.3]);
    const [ref] = referenceSeries([sparse, dense], 1, 'energy_reference_timeseries');
    expect(ref.x).toEqual([1, 2, 3]);
  });

  it('is empty without samples', () => {
    expect(referenceSeries([], 1, 'energy_reference_timeseries')).toEqual([]);
  });
});
