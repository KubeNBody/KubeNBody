// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import type { LineSeries } from '@/components/charts/LineChart';
import { type ComparisonLabels, jobWithLabel } from '@/lib/domain/comparison-labels';
import { CHART, seriesStyle } from '@/lib/theme/chart';
import type { SingleJobComparisonResult } from '@/types/api';

type Field = 'position_rmse_timeseries' | 'velocity_rmse_timeseries' | 'energy_comparison_timeseries' | 'momentum_comparison_timeseries';

type Transform = (values: number[], result: SingleJobComparisonResult) => number[];

/** One line per compared job, coloured by its position in the comparison (stable, not by rank) */
export function comparisonSeries(
  comparisons: SingleJobComparisonResult[],
  field: Field,
  labels?: ComparisonLabels,
  transform: Transform = (v) => v
): LineSeries[] {
  return comparisons.map((c, i) => ({
    id: `job-${c.job_id}`,
    label: jobWithLabel(c.job_id, labels?.jobs[c.job_id]),
    ...seriesStyle(i),
    x: c.simulation_times,
    y: transform(c[field], c),
  }));
}

/**
 * The reference job's own series. Each comparison result samples it at the
 * times it shares with that job, so the one with the most samples is drawn.
 */
export function referenceSeries(
  comparisons: SingleJobComparisonResult[],
  referenceJobId: number,
  field: 'energy_reference_timeseries' | 'momentum_reference_timeseries',
  labels?: ComparisonLabels,
  transform: Transform = (v) => v
): LineSeries[] {
  const densest = comparisons.reduce<SingleJobComparisonResult | undefined>(
    (best, c) => ((c[field]?.length ?? 0) > (best?.[field]?.length ?? 0) ? c : best),
    undefined
  );
  if (!densest?.[field]?.length) return [];
  return [{ id: 'reference', label: `Reference ${jobWithLabel(referenceJobId, labels?.reference)}`, color: CHART.reference, primary: true, x: densest.simulation_times, y: transform(densest[field], densest) }];
}

/**
 * |E − E₀| / |E₀| for each sample. E₀ is the job's energy at t = 0 when the
 * result has it, otherwise the series' first sample (results stored before E₀
 * was recorded).
 */
export function relativeDrift(values: number[], e0: number | null | undefined = values[0]): number[] {
  const base = e0 ?? values[0];
  return values.map((e) => (base ? Math.abs((e - base) / base) : NaN));
}
