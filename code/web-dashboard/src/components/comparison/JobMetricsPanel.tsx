// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { Activity } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { LoadingSpinner } from '@/components/ui/loading-spinner';
import { EnergyChart } from '@/components/comparison/EnergyChart';
import { EnergyDriftChart } from '@/components/comparison/EnergyDriftChart';
import { DriftBadge } from '@/components/jobs/badges';
import { useJobMetrics } from '@/hooks/use-api';

/** One job's conservation metrics: energy and its drift (compare page, job page) */
export function JobMetricsPanel({ jobId }: { jobId: number }) {
  const { data: metrics, isLoading, isError, error } = useJobMetrics(jobId);

  if (isLoading) {
    return (
      <Card className="surface-card border-border no-print">
        <CardHeader>
          <CardTitle className="text-fg">Job #{jobId} Metrics</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center justify-center py-12">
            <LoadingSpinner />
            <span className="ml-3 text-fg-muted">Computing metrics...</span>
          </div>
        </CardContent>
      </Card>
    );
  }

  if (isError) {
    return (
      <Card className="surface-card border-border border-danger/40 no-print">
        <CardHeader>
          <CardTitle className="text-fg">Job #{jobId} Metrics</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-danger">Failed to load metrics: {error?.message || 'Unknown error'}</p>
        </CardContent>
      </Card>
    );
  }

  if (!metrics) {
    return null;
  }

  return (
    <Card className="surface-card border-border print-section">
      <CardHeader>
        <CardTitle className="text-fg flex items-center gap-2">
          <Activity className="h-5 w-5" />
          Job #{jobId} Conservation Metrics
        </CardTitle>
        <CardDescription className="text-fg-muted">
          {metrics.epochs.length} snapshots analyzed in {metrics.computation_time_ms}ms
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Summary badges */}
        <div className="flex flex-wrap gap-3">
          <DriftBadge percent={metrics.energy_drift_percent} label="Energy Drift" />
        </div>

        {/* Charts */}
        <div className="grid grid-cols-1 gap-6">
          <EnergyChart metrics={metrics} />
          <EnergyDriftChart metrics={metrics} />
        </div>
      </CardContent>
    </Card>
  );
}

// Summary comparison section
