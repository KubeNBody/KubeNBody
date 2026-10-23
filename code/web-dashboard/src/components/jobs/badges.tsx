// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { cn } from '@/lib/utils';
import { DIMENSION_BADGE, forceAlgorithmLabel, jobTypeLabel, type Dimension } from '@/lib/domain/badges';
import { comparisonStatusInfo } from '@/lib/domain/comparison-status';
import { driftQuality } from '@/lib/domain/drift';
import { jobStatusInfo } from '@/lib/domain/job-status';
import { TONE_BADGE } from '@/lib/domain/tones';
import { integrationMethodLabel, partitionStrategyLabel } from '@/lib/format-integration';

// The one set of badges every page uses: statuses on the status tones
// (lib/domain/tones.ts), identities on one hue per dimension (lib/domain/badges.ts).

const PILL = 'inline-flex items-center gap-1 whitespace-nowrap rounded border px-2 py-0.5 text-xs font-medium';

export function StatusBadge({ status, className }: { status: string; className?: string }) {
  const s = jobStatusInfo(status);
  const Icon = s.icon;
  return (
    <span className={cn(PILL, s.badge, className)}>
      <Icon className="h-3 w-3" aria-hidden />
      {s.label}
    </span>
  );
}

export function ComparisonStatusBadge({ status, className }: { status: string; className?: string }) {
  const s = comparisonStatusInfo(status);
  const Icon = s.icon;
  return (
    <span className={cn(PILL, s.badge, className)}>
      <Icon className={cn('h-3 w-3', s.spin && 'animate-spin')} aria-hidden />
      {s.label}
    </span>
  );
}

function DimensionBadge({ dimension, children, className }: { dimension: Dimension; children: React.ReactNode; className?: string }) {
  return <span className={cn(PILL, DIMENSION_BADGE[dimension], className)}>{children}</span>;
}

export function JobTypeBadge({ jobType, className }: { jobType: string; className?: string }) {
  return <DimensionBadge dimension="jobType" className={className}>{jobTypeLabel(jobType)}</DimensionBadge>;
}

export function AlgorithmBadge({ algorithm, long, className }: { algorithm?: string | null; long?: boolean; className?: string }) {
  return <DimensionBadge dimension="algorithm" className={className}>{forceAlgorithmLabel(algorithm, long)}</DimensionBadge>;
}

export function IntegratorBadge({ method, className }: { method?: string | null; className?: string }) {
  return <DimensionBadge dimension="integrator" className={className}>{integrationMethodLabel(method)}</DimensionBadge>;
}

export function PartitionBadge({ strategy, className }: { strategy?: string | null; className?: string }) {
  return <DimensionBadge dimension="partition" className={className}>{partitionStrategyLabel(strategy)}</DimensionBadge>;
}

export function GpuBadge({ className }: { className?: string }) {
  return <DimensionBadge dimension="gpu" className={cn('font-semibold', className)}>GPU</DimensionBadge>;
}

/** Relative energy drift |ΔE/E₀| with its quality tone (lib/domain/drift.ts) */
export function DriftBadge({ percent, label = 'Energy drift', className }: { percent: number; label?: string; className?: string }) {
  const q = driftQuality(percent);
  return (
    <span className={cn(PILL, TONE_BADGE[q.tone], className)} title={`${q.label} energy conservation`}>
      {label}: {Number.isFinite(percent) ? `${percent.toPrecision(3)}%` : '—'}
    </span>
  );
}
