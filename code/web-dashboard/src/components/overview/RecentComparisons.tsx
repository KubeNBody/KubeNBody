// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import Link from 'next/link';
import { ArrowRight, GitCompare } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { ComparisonStatusBadge } from '@/components/jobs/badges';
import { LocalTime } from '@/components/LocalTime';
import { useComparisonJobs } from '@/hooks/use-api';

/** The latest saved comparisons (client-side: they are read through the /api proxy) */
export function RecentComparisons() {
  const { data, isLoading, isError } = useComparisonJobs(1);
  const comparisons = data?.comparisons.slice(0, 5) ?? [];

  return (
    <section aria-labelledby="recent-comparisons" className="rounded-lg border border-border bg-surface">
      <div className="flex items-center justify-between px-5 pt-4 pb-2">
        <h2 id="recent-comparisons" className="text-sm font-medium text-fg">
          Recent comparisons
        </h2>
        <Button asChild variant="ghost" size="sm" className="text-fg-muted hover:text-fg">
          <Link href="/dashboard/comparisons">
            All <ArrowRight />
          </Link>
        </Button>
      </div>
      <div className="px-3 pb-3">
        {isLoading ? (
          <div className="space-y-2 px-2 py-1">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-10 w-full bg-surface-raised" />
            ))}
          </div>
        ) : isError ? (
          <p className="px-2 py-3 text-sm text-danger">Could not load comparisons.</p>
        ) : comparisons.length === 0 ? (
          <div className="px-2 py-6 text-center">
            <GitCompare className="mx-auto mb-2 size-8 text-fg-subtle" aria-hidden />
            <p className="mb-3 text-sm text-fg-muted">No saved comparisons yet.</p>
            <Button asChild variant="outline" size="sm">
              <Link href="/dashboard/compare">Compare runs</Link>
            </Button>
          </div>
        ) : (
          <ul>
            {comparisons.map((c) => (
              <li key={c.comparison_id}>
                <Link
                  href={`/dashboard/comparisons/${c.comparison_id}`}
                  className="flex items-center justify-between gap-3 rounded-md px-2 py-2 transition-colors hover:bg-surface-raised"
                >
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-medium text-fg">
                      #{c.reference_job_id} vs {c.comparison_job_ids.map((id) => `#${id}`).join(', ')}
                    </span>
                    <span className="text-xs text-fg-subtle">
                      Comparison {c.comparison_id} · <LocalTime value={c.created_at} format="date" />
                    </span>
                  </span>
                  <ComparisonStatusBadge status={c.status} />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
