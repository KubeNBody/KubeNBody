// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { LineChart } from '@/components/charts/LineChart';
import { SERIES_COLORS } from '@/lib/theme/chart';
import type { JobMetricsResponse } from '@/types/api';

/**
 * Relative energy error |E − E₀| / |E₀| (in percent) on a log axis: the error
 * spans orders of magnitude, which a linear axis flattens to a line at zero.
 */
export function EnergyDriftChart({ metrics }: { metrics: JobMetricsResponse }) {
  return (
    <LineChart
      title="Relative energy error"
      description="Magnitude of the relative energy error, in percent, over simulation time, on a logarithmic axis."
      xLabel="Simulation time"
      yLabel="|ΔE / E₀| (%)"
      yScale="log"
      series={[
        {
          id: 'drift',
          label: 'Energy error',
          color: SERIES_COLORS[0],
          x: metrics.metrics.map((m) => m.simulation_time),
          y: metrics.metrics.map((m) => Math.abs(m.relative_energy_error) * 100),
        },
      ]}
    />
  );
}
