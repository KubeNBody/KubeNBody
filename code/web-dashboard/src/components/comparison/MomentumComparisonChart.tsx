// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { LineChart } from '@/components/charts/LineChart';
import type { ComparisonLabels } from '@/lib/domain/comparison-labels';
import type { SingleJobComparisonResult } from '@/types/api';
import { comparisonSeries, referenceSeries } from './series';

/** Total linear momentum |P| of the reference and each job (log axis: round-off sized values) */
export function MomentumComparisonChart({ comparisons, referenceJobId, labels }: { comparisons: SingleJobComparisonResult[]; referenceJobId: number; labels: ComparisonLabels }) {
  return (
    <LineChart
      title="Linear momentum |P|"
      description="Magnitude of the total linear momentum of the reference and each job over simulation time, logarithmic axis."
      xLabel="Simulation time"
      yLabel="|P|"
      yScale="log"
      series={[
        ...referenceSeries(comparisons, referenceJobId, 'momentum_reference_timeseries', labels),
        ...comparisonSeries(comparisons, 'momentum_comparison_timeseries', labels),
      ]}
    />
  );
}
