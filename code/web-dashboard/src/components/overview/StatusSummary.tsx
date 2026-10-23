// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import Link from 'next/link';
import { statusConfig } from '@/lib/domain/job-status';
import { TONE_FILL } from '@/lib/domain/tones';
import { cn, formatCount } from '@/lib/utils';

/**
 * Job counts by status as one proportional bar plus a legend. Each status links
 * to the jobs list filtered on it.
 */
export function StatusSummary({
  counts,
  total,
  sampled,
}: {
  counts: Record<string, number>;
  total: number;
  /** How many jobs the counts cover (the backend lists at most 100 per request) */
  sampled: number;
}) {
  const entries = Object.entries(statusConfig).map(([status, info]) => ({ status, info, count: counts[status] ?? 0 }));
  const counted = entries.reduce((sum, e) => sum + e.count, 0);

  return (
    <section aria-labelledby="status-summary" className="rounded-lg border border-border bg-surface p-5">
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="status-summary" className="text-sm font-medium text-fg">
          Jobs by status
        </h2>
        <p className="text-xs text-fg-muted">
          {formatCount(total)} jobs in total
          {total > sampled && ` · counts cover the latest ${formatCount(sampled)}`}
        </p>
      </div>

      {counted > 0 && (
        <div className="mb-4 flex h-2 overflow-hidden rounded-full bg-surface-raised" aria-hidden>
          {entries
            .filter((e) => e.count > 0)
            .map((e) => (
              <div key={e.status} className={cn('h-full', TONE_FILL[e.info.tone])} style={{ width: `${(e.count / counted) * 100}%` }} />
            ))}
        </div>
      )}

      <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        {entries.map(({ status, info, count }) => {
          const Icon = info.icon;
          return (
            <li key={status}>
              <Link
                href={`/dashboard/jobs?status=${status}`}
                className="flex items-center gap-2.5 rounded-md px-2.5 py-2 transition-colors hover:bg-surface-raised"
              >
                <Icon className={cn('size-4 shrink-0', info.color)} aria-hidden />
                <span className="text-lg font-semibold tabular-nums text-fg">{formatCount(count)}</span>
                <span className="truncate text-sm text-fg-muted">{info.label}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
