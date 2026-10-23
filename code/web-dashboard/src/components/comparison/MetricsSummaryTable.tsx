// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useJobMetrics } from '@/hooks/use-api';
import { LoadingSpinner } from '@/components/ui/loading-spinner';
import { CheckCircle, XCircle, AlertCircle, type LucideIcon } from 'lucide-react';
import { driftQuality } from '@/lib/domain/drift';
import { TONE_BADGE, type Tone } from '@/lib/domain/tones';

const DRIFT_ICON: Record<Tone, LucideIcon> = {
  success: CheckCircle,
  info: CheckCircle,
  warning: AlertCircle,
  caution: AlertCircle,
  danger: XCircle,
  neutral: AlertCircle,
};

interface MetricsSummaryTableProps {
  jobIds: number[];
}

export function MetricsSummaryTable({ jobIds }: MetricsSummaryTableProps) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border">
            <th className="text-left py-3 px-3 text-fg-muted font-medium">Job ID</th>
            <th className="text-left py-3 px-3 text-fg-muted font-medium">Snapshots</th>
            <th className="text-left py-3 px-3 text-fg-muted font-medium">Energy Drift</th>
            <th className="text-left py-3 px-3 text-fg-muted font-medium">Compute Time</th>
          </tr>
        </thead>
        <tbody>
          {jobIds.map(jobId => (
            <MetricsSummaryRow key={jobId} jobId={jobId} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MetricsSummaryRow({ jobId }: { jobId: number }) {
  const { data: metrics, isLoading, isError } = useJobMetrics(jobId);

  if (isLoading) {
    return (
      <tr className="border-b border-border">
        <td className="py-3 px-3 text-fg font-mono">#{jobId}</td>
        <td colSpan={3} className="py-3 px-3">
          <div className="flex items-center gap-2 text-fg-muted">
            <LoadingSpinner size="sm" />
            Computing metrics...
          </div>
        </td>
      </tr>
    );
  }

  if (isError || !metrics) {
    return (
      <tr className="border-b border-border">
        <td className="py-3 px-3 text-fg font-mono">#{jobId}</td>
        <td colSpan={3} className="py-3 px-3 text-danger">
          Failed to load metrics
        </td>
      </tr>
    );
  }

  const drift = driftQuality(metrics.energy_drift_percent);
  const DriftIcon = DRIFT_ICON[drift.tone];

  return (
    <tr className="border-b border-border hover:bg-surface-raised">
      <td className="py-3 px-3 text-fg font-mono">#{jobId}</td>
      <td className="py-3 px-3 text-fg-muted">{metrics.epochs.length}</td>
      <td className="py-3 px-3">
        <span
          className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs font-medium ${TONE_BADGE[drift.tone]}`}
          title={`${drift.label} energy conservation`}
        >
          <DriftIcon className="h-3 w-3" aria-hidden />
          {metrics.energy_drift_percent.toFixed(4)}%
        </span>
      </td>
      <td className="py-3 px-3 text-fg-muted font-mono text-xs">
        {metrics.computation_time_ms}ms
      </td>
    </tr>
  );
}
