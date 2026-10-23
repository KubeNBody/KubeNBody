// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useState } from 'react';
import Link from 'next/link';
import { GitCompare, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Pagination } from '@/components/ui/pagination';
import { Skeleton } from '@/components/ui/skeleton';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { ComparisonStatusBadge } from '@/components/jobs/badges';
import { PageHeader } from '@/components/layout/PageHeader';
import { LocalTime } from '@/components/LocalTime';
import { useComparisonJobs, useDeleteComparisonJob } from '@/hooks/use-api';
import { capabilities } from '@/lib/capabilities';
import { TONE_BADGE } from '@/lib/domain/tones';
import { formatCount } from '@/lib/utils';
import type { ComparisonJobResponse } from '@/types/api';

const PAGE_SIZE = 20;

export default function ComparisonsPage() {
  const [page, setPage] = useState(1);
  const { data, isLoading, isError } = useComparisonJobs(page);
  const remove = useDeleteComparisonJob();
  const [deleting, setDeleting] = useState<ComparisonJobResponse | null>(null);

  return (
    <div>
      <PageHeader
        title="Saved comparisons"
        description={data ? `${formatCount(data.total)} comparison${data.total === 1 ? '' : 's'}, computed in the background and kept for later` : 'Computed in the background and kept for later'}
        actions={
          <Button asChild>
            <Link href="/dashboard/compare">
              <GitCompare /> New comparison
            </Link>
          </Button>
        }
      />

      {isLoading ? (
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-12 w-full bg-surface" />
          ))}
        </div>
      ) : isError ? (
        <p role="alert" className="rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
          Could not load the saved comparisons.
        </p>
      ) : !data?.comparisons.length ? (
        <div className="rounded-lg border border-border bg-surface px-6 py-14 text-center">
          <GitCompare className="mx-auto mb-3 size-10 text-fg-subtle" aria-hidden />
          <p className="font-medium text-fg">No saved comparisons yet</p>
          <p className="text-sm text-fg-muted">On the Compare page, “Save and run in background” keeps a comparison here.</p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-surface">
          <Table>
            <TableHeader>
              <TableRow className="border-border hover:bg-transparent">
                <TableHead className="pl-4 text-fg-muted">Comparison</TableHead>
                <TableHead className="text-fg-muted">Status</TableHead>
                <TableHead className="hidden text-right text-fg-muted sm:table-cell">Compared</TableHead>
                <TableHead className="hidden text-right text-fg-muted md:table-cell">Took</TableHead>
                <TableHead className="hidden text-right text-fg-muted md:table-cell">Created</TableHead>
                <TableHead className="w-10 pr-3">
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.comparisons.map((c) => (
                <TableRow key={c.comparison_id} className="border-border hover:bg-surface-raised">
                  <TableCell className="pl-4">
                    <Link href={`/dashboard/comparisons/${c.comparison_id}`} className="font-medium text-fg hover:text-link">
                      #{c.reference_job_id} vs {c.comparison_job_ids.map((id) => `#${id}`).join(', ')}
                    </Link>
                    <p className="text-xs tabular-nums text-fg-subtle">Comparison {c.comparison_id}</p>
                  </TableCell>
                  <TableCell>
                    <span className="flex flex-wrap items-center gap-1.5">
                      <ComparisonStatusBadge status={c.status} />
                      {c.stale && (
                        <span className={`rounded border px-2 py-0.5 text-xs ${TONE_BADGE.warning}`} title={c.stale_reason}>
                          Stale
                        </span>
                      )}
                    </span>
                  </TableCell>
                  <TableCell className="hidden text-right tabular-nums text-fg-muted sm:table-cell">
                    {c.jobs_compared} of {c.jobs_compared + c.jobs_skipped}
                  </TableCell>
                  <TableCell className="hidden text-right tabular-nums text-fg-muted md:table-cell">
                    {c.computation_time_ms != null ? `${(c.computation_time_ms / 1000).toFixed(1)} s` : '—'}
                  </TableCell>
                  <TableCell className="hidden text-right text-fg-muted md:table-cell">
                    <LocalTime value={c.created_at} format="date" />
                  </TableCell>
                  <TableCell className="pr-3 text-right">
                    {capabilities.canMutate && (
                      <Button variant="ghost" size="icon-sm" onClick={() => setDeleting(c)} aria-label={`Delete comparison ${c.comparison_id}`} className="text-fg-muted hover:text-danger">
                        <Trash2 />
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {data && (
        <div className="mt-4">
          <Pagination currentPage={page} pages={data.pages} total={data.total} pageSize={PAGE_SIZE} itemLabel="comparisons" onPageChange={setPage} />
        </div>
      )}

      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete comparison ${deleting?.comparison_id ?? ''}?`}
        description="The saved results are removed, the jobs themselves are not touched."
        confirmLabel="Delete"
        destructive
        pending={remove.isPending}
        onConfirm={() =>
          deleting &&
          remove.mutate(deleting.comparison_id, {
            onSuccess: () => {
              toast.success('Comparison deleted');
              setDeleting(null);
            },
            onError: (e) => toast.error('Could not delete the comparison', { description: e.message }),
          })
        }
      />
    </div>
  );
}
