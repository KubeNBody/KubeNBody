// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useState } from 'react';
import { LineChart } from '@/components/charts/LineChart';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import type { ComparisonLabels } from '@/lib/domain/comparison-labels';
import type { SingleJobComparisonResult } from '@/types/api';
import { comparisonSeries, referenceSeries, relativeDrift } from './series';

/**
 * Energy of the reference and each compared job. By default the relative
 * error |E − E₀| / |E₀| on a log axis (a drifting run otherwise squashes the
 * others into one flat line). The absolute energy is one click away.
 */
export function EnergyComparisonChart({ comparisons, referenceJobId, labels }: { comparisons: SingleJobComparisonResult[]; referenceJobId: number; labels: ComparisonLabels }) {
  const [view, setView] = useState<'relative' | 'absolute'>('relative');
  const relative = view === 'relative';
  const referenceDrift = relative
    ? (v: number[], c: SingleJobComparisonResult) => relativeDrift(v, c.energy_initial_reference)
    : undefined;
  const comparisonDrift = relative
    ? (v: number[], c: SingleJobComparisonResult) => relativeDrift(v, c.energy_initial_comparison)
    : undefined;
  return (
    <div className="space-y-2">
      <ToggleGroup type="single" value={view} onValueChange={(v) => v && setView(v as typeof view)} className="rounded-md border border-border bg-surface p-0.5" aria-label="Energy view">
        <ToggleGroupItem value="relative" size="sm" className="px-3 text-xs text-fg-muted data-[state=on]:bg-surface-raised data-[state=on]:text-fg">
          Relative error
        </ToggleGroupItem>
        <ToggleGroupItem value="absolute" size="sm" className="px-3 text-xs text-fg-muted data-[state=on]:bg-surface-raised data-[state=on]:text-fg">
          Total energy
        </ToggleGroupItem>
      </ToggleGroup>
      <LineChart
        title={relative ? 'Relative energy error' : 'Total energy'}
        description={
          relative
            ? 'Relative energy error of the reference and each job against its own initial energy, logarithmic axis.'
            : 'Total energy of the reference and each job over simulation time.'
        }
        xLabel="Simulation time"
        yLabel={relative ? '|ΔE / E₀|' : 'Total energy'}
        yScale={relative ? 'log' : 'linear'}
        series={[
          ...referenceSeries(comparisons, referenceJobId, 'energy_reference_timeseries', labels, referenceDrift),
          ...comparisonSeries(comparisons, 'energy_comparison_timeseries', labels, comparisonDrift),
        ]}
      />
    </div>
  );
}
