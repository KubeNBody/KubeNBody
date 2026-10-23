// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { ChevronLeft, ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn, formatCount, getPageNumbers } from '@/lib/utils';

// Numbered pager of the server-paginated lists (jobs, files, comparisons).
// Renders nothing for a single page
export function Pagination({
  currentPage,
  pages,
  total,
  pageSize,
  itemLabel,
  onPageChange,
}: {
  currentPage: number;
  pages: number;
  total: number;
  pageSize: number;
  // Plural noun for the "1–20 of 45 jobs" line
  itemLabel: string;
  onPageChange: (page: number) => void;
}) {
  if (pages <= 1) return null;
  const first = (currentPage - 1) * pageSize + 1;
  const last = Math.min(currentPage * pageSize, total);

  return (
    <nav aria-label="Pagination" className="flex flex-col items-center justify-between gap-3 sm:flex-row">
      <p className="text-sm text-fg-muted">
        {formatCount(first)}–{formatCount(last)} of {formatCount(total)} {itemLabel}
      </p>
      <div className="flex items-center gap-1">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onPageChange(currentPage - 1)}
          disabled={currentPage <= 1}
          className="text-fg-muted hover:text-fg"
        >
          <ChevronLeft /> Previous
        </Button>
        {getPageNumbers(currentPage, pages).map((p, idx) =>
          p === '...' ? (
            <span key={`gap-${idx}`} className="px-2 text-fg-subtle" aria-hidden>
              …
            </span>
          ) : (
            <Button
              key={p}
              variant="ghost"
              size="icon-sm"
              onClick={() => onPageChange(Number(p))}
              aria-label={`Page ${p}`}
              aria-current={currentPage === p ? 'page' : undefined}
              className={cn('tabular-nums', currentPage === p ? 'bg-primary/15 text-fg' : 'text-fg-muted hover:text-fg')}
            >
              {p}
            </Button>
          )
        )}
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onPageChange(currentPage + 1)}
          disabled={currentPage >= pages}
          className="text-fg-muted hover:text-fg"
        >
          Next <ChevronRight />
        </Button>
      </div>
    </nav>
  );
}
