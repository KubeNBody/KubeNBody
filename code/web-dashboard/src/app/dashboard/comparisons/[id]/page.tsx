// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useParams } from 'next/navigation';
import { AlertTriangle, Loader2, XCircle } from 'lucide-react';
import { LoadingSpinner } from '@/components/ui/loading-spinner';
import { ComparisonResultsView } from '@/components/comparison';
import { ComparisonStatusBadge } from '@/components/jobs/badges';
import { PageHeader } from '@/components/layout/PageHeader';
import { useComparisonJob } from '@/hooks/use-api';
import { comparisonTitle } from '@/lib/domain/comparison-labels';

function StateMessage({ icon: Icon, tone, title, children }: { icon: typeof XCircle; tone: string; title: string; children?: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-surface px-6 py-14 text-center">
      <Icon className={`mx-auto mb-3 size-10 ${tone}`} aria-hidden />
      <p className={`font-medium ${tone}`}>{title}</p>
      {children && <div className="mt-1 text-sm text-fg-muted">{children}</div>}
    </div>
  );
}

export default function ComparisonDetailPage() {
  const comparisonId = Number(useParams().id);
  const { data: c, isLoading, isError } = useComparisonJob(comparisonId);
  const back = { href: '/dashboard/comparisons', label: 'Saved comparisons' };

  if (isLoading) {
    return (
      <div className="flex justify-center py-20" role="status">
        <LoadingSpinner size="lg" />
      </div>
    );
  }
  if (isError || !c) {
    return (
      <div>
        <PageHeader back={back} title={`Comparison ${comparisonId}`} />
        <StateMessage icon={XCircle} tone="text-danger" title="Could not load this comparison" />
      </div>
    );
  }

  const header = (
    <PageHeader
      back={back}
      title={c.results?.comparisons.length ? comparisonTitle(c.results) : `#${c.reference_job_id} vs ${c.comparison_job_ids.map((id) => `#${id}`).join(', ')}`}
      description={
        <span className="flex flex-wrap items-center gap-2">
          <span className="tabular-nums">Comparison {c.comparison_id}</span>
          <ComparisonStatusBadge status={c.status} />
          {c.computation_time_ms != null && <span className="tabular-nums">computed in {(c.computation_time_ms / 1000).toFixed(1)} s</span>}
        </span>
      }
    />
  );

  return (
    <div>
      {header}
      {c.stale && (
        <p role="status" className="mb-6 flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 p-3 text-sm text-warning">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          {c.stale_reason || 'A job changed after this comparison was computed'}: the results may not match the jobs any more.
        </p>
      )}
      {c.status === 'PENDING' || c.status === 'COMPUTING' ? (
        <StateMessage icon={Loader2} tone="text-info" title={c.status === 'PENDING' ? 'Waiting to start…' : 'Computing…'}>
          This page updates by itself.
        </StateMessage>
      ) : c.status === 'FAILED' ? (
        <StateMessage icon={XCircle} tone="text-danger" title="The comparison failed">
          {c.error_message}
        </StateMessage>
      ) : !c.results || c.status === 'COMPLETED_EMPTY' ? (
        <StateMessage icon={AlertTriangle} tone="text-warning" title="No comparable data">
          {c.results?.skipped_jobs?.map((s) => (
            <p key={s.job_id}>
              #{s.job_id}: {s.reason}
            </p>
          )) ?? 'The comparison finished without results.'}
        </StateMessage>
      ) : (
        <ComparisonResultsView results={c.results} />
      )}
    </div>
  );
}
