// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { LineChart } from '@/components/charts/LineChart';
import { CHART, SERIES_COLORS } from '@/lib/theme/chart';
import type { JobMetricsResponse } from '@/types/api';

/** One job's total, kinetic and potential energy over simulation time */
export function EnergyChart({ metrics }: { metrics: JobMetricsResponse }) {
  const t = metrics.metrics.map((m) => m.simulation_time);
  return (
    <LineChart
      title="Energy"
      description="Total, kinetic and potential energy of the system over simulation time."
      xLabel="Simulation time"
      yLabel="Energy"
      series={[
        { id: 'total', label: 'Total', color: CHART.reference, primary: true, x: t, y: metrics.metrics.map((m) => m.total_energy) },
        { id: 'kinetic', label: 'Kinetic', color: SERIES_COLORS[0], x: t, y: metrics.metrics.map((m) => m.kinetic_energy) },
        { id: 'potential', label: 'Potential', color: SERIES_COLORS[1], x: t, y: metrics.metrics.map((m) => m.potential_energy) },
      ]}
    />
  );
}
