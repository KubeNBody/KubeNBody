// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { LineChart } from '@/components/charts/LineChart';
import type { ComparisonLabels } from '@/lib/domain/comparison-labels';
import type { SingleJobComparisonResult } from '@/types/api';
import { comparisonSeries } from './series';

/** Position and velocity RMSE of each job against the reference, log axes (they span many decades) */
export function TrajectoryDivergenceChart({ comparisons, labels }: { comparisons: SingleJobComparisonResult[]; labels: ComparisonLabels }) {
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <LineChart
        title="Position RMSE vs reference"
        description="Root-mean-square position difference of each job from the reference over simulation time, logarithmic axis."
        xLabel="Simulation time"
        yLabel="Position RMSE"
        yScale="log"
        series={comparisonSeries(comparisons, 'position_rmse_timeseries', labels)}
      />
      <LineChart
        title="Velocity RMSE vs reference"
        description="Root-mean-square velocity difference of each job from the reference over simulation time, logarithmic axis."
        xLabel="Simulation time"
        yLabel="Velocity RMSE"
        yScale="log"
        series={comparisonSeries(comparisons, 'velocity_rmse_timeseries', labels)}
      />
    </div>
  );
}
