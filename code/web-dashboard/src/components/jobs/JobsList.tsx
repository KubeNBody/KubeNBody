// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { GitCompare, Inbox } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Pagination } from '@/components/ui/pagination';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { AlgorithmBadge, GpuBadge, IntegratorBadge, JobTypeBadge, StatusBadge } from '@/components/jobs/badges';
import { JobActionsMenu } from '@/components/jobs/JobActionsMenu';
import { PageHeader } from '@/components/layout/PageHeader';
import { ClearFilters, FilterSelect, PageSizeSelect, SearchInput } from '@/components/list/ListToolbar';
import { RefreshControls } from '@/components/list/RefreshControls';
import { Duration, LocalTime } from '@/components/LocalTime';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { capabilities } from '@/lib/capabilities';
import { JOB_TYPES } from '@/lib/domain/badges';
import { statusConfig } from '@/lib/domain/job-status';
import { ALL_INTEGRATION_METHODS, integrationMethodLabel } from '@/lib/format-integration';
import { formatSofteningSq } from '@/lib/format-softening';
import { cn, formatCount, jobRunWindow } from '@/lib/utils';
import type { Job } from '@/types/api';
import { jobTitle } from '@/lib/domain/job-title';

const STATUS_OPTIONS = Object.entries(statusConfig).map(([value, s]) => ({ value, label: s.label }));
const INTEGRATOR_OPTIONS = ALL_INTEGRATION_METHODS.map((value) => ({ value, label: integrationMethodLabel(value) }));
const FILTER_KEYS = ['status', 'job_type', 'integration_method', 'search'] as const;

export interface JobsListProps {
  /** The current page of jobs, already filtered (server: from the URL, demo: in the browser) */
  jobs: Job[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

/** Jobs list: filters in the URL, a table on wide screens and cards on phones */
export function JobsList({ jobs, total, page, size, pages }: JobsListProps) {
  const { searchParams, currentPage, currentSize, pastLastPage, handleFilterChange, handleSearchChange, clearFilters } = useUrlFilters(
    '/dashboard/jobs',
    { initialPage: page, initialSize: size, total, pages }
  );
  const filter = (key: (typeof FILTER_KEYS)[number]) => searchParams.get(key) || '';
  const filtered = FILTER_KEYS.some((k) => filter(k));

  // Bumped by "Clear filters" to remount the (uncontrolled) search box empty
  const [searchKey, setSearchKey] = useState(0);

  // Completed jobs selected for comparison (the first one becomes the reference)
  const [selected, setSelected] = useState<number[]>([]);
  const selectable = useMemo(() => jobs.filter((j) => j.status === 'COMPLETED').map((j) => j.job_id), [jobs]);
  const toggle = (id: number) => setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
  const allOnPage = selectable.length > 0 && selectable.every((id) => selected.includes(id));
  const compareHref = `/dashboard/compare?ref=${selected[0]}&jobs=${selected.slice(1).join(',')}`;

  return (
    <div>
      <PageHeader
        title="Jobs"
        description={`${formatCount(total)} ${filtered ? 'matching ' : ''}job${total === 1 ? '' : 's'}`}
        actions={
          <>
            <RefreshControls label="Refresh jobs" autoRefresh={capabilities.liveData} />
            <Button asChild>
              <Link href="/dashboard/submit">Submit job</Link>
            </Button>
          </>
        }
      />

      {/* Filters: one row that wraps */}
      <div className="mb-4 flex flex-wrap items-center gap-2" role="search">
        <SearchInput
          key={searchKey}
          defaultValue={filter('search')}
          onChange={handleSearchChange}
          placeholder="Search name, description or #id"
          label="Search jobs"
        />
        <FilterSelect label="Status" value={filter('status')} options={STATUS_OPTIONS} allLabel="All statuses" onChange={(v) => handleFilterChange('status', v)} />
        <FilterSelect label="Job type" value={filter('job_type')} options={JOB_TYPES} allLabel="All types" onChange={(v) => handleFilterChange('job_type', v)} />
        <FilterSelect
          label="Integrator"
          value={filter('integration_method')}
          options={INTEGRATOR_OPTIONS}
          allLabel="All integrators"
          onChange={(v) => handleFilterChange('integration_method', v)}
        />
        <ClearFilters
          visible={filtered}
          onClear={() => {
            clearFilters(FILTER_KEYS);
            setSearchKey((k) => k + 1);
          }}
        />
        <div className="ml-auto">
          <PageSizeSelect value={currentSize} onChange={(s) => handleFilterChange('size', String(s))} />
        </div>
      </div>

      {/* Selection bar */}
      {selected.length > 0 && (
        <div className="mb-3 flex flex-wrap items-center gap-3 rounded-md border border-primary/40 bg-primary/10 px-4 py-2 text-sm" role="status">
          <span className="text-fg">
            {selected.length} selected · reference <span className="font-medium">#{selected[0]}</span>
          </span>
          {selected.length < 2 ? (
            <span className="text-fg-muted">Select one more completed job to compare</span>
          ) : (
            <Button asChild size="sm">
              <Link href={compareHref}>
                <GitCompare /> Compare {selected.length} jobs
              </Link>
            </Button>
          )}
          <Button variant="ghost" size="sm" onClick={() => setSelected([])} className="text-fg-muted hover:text-fg">
            Clear selection
          </Button>
        </div>
      )}

      {pastLastPage ? null : jobs.length === 0 ? (
        <div className="rounded-lg border border-border bg-surface px-6 py-14 text-center">
          <Inbox className="mx-auto mb-3 size-10 text-fg-subtle" aria-hidden />
          <p className="font-medium text-fg">No jobs found</p>
          <p className="mb-4 text-sm text-fg-muted">{filtered ? 'Try other filters.' : 'Submit a simulation to get started.'}</p>
          {!filtered && (
            <Button asChild>
              <Link href="/dashboard/submit">Submit a job</Link>
            </Button>
          )}
        </div>
      ) : (
        <>
          {/* Wide screens: table */}
          <div className="hidden overflow-hidden rounded-lg border border-border bg-surface md:block">
            <Table>
              <TableHeader>
                <TableRow className="border-border hover:bg-transparent">
                  <TableHead className="w-10 pl-4">
                    <Checkbox
                      checked={allOnPage}
                      disabled={selectable.length === 0}
                      onCheckedChange={(c) => setSelected(c ? Array.from(new Set([...selected, ...selectable])) : selected.filter((id) => !selectable.includes(id)))}
                      aria-label="Select all completed jobs on this page"
                    />
                  </TableHead>
                  <TableHead className="text-fg-muted">Job</TableHead>
                  <TableHead className="text-fg-muted">Status</TableHead>
                  <TableHead className="text-fg-muted">Type</TableHead>
                  <TableHead className="hidden text-fg-muted xl:table-cell">Algorithm</TableHead>
                  <TableHead className="text-fg-muted">Integrator</TableHead>
                  <TableHead className="hidden text-right text-fg-muted lg:table-cell">Epochs</TableHead>
                  <TableHead className="hidden text-right text-fg-muted lg:table-cell">dt</TableHead>
                  <TableHead className="hidden text-right text-fg-muted 2xl:table-cell">ε²</TableHead>
                  <TableHead className="text-right text-fg-muted">Duration</TableHead>
                  <TableHead className="hidden text-right text-fg-muted lg:table-cell">Created</TableHead>
                  <TableHead className="w-10 pr-3">
                    <span className="sr-only">Actions</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {jobs.map((job) => (
                  <JobRow key={job.job_id} job={job} selected={selected.includes(job.job_id)} onToggle={toggle} />
                ))}
              </TableBody>
            </Table>
          </div>

          {/* Phones: cards */}
          <ul className="space-y-2 md:hidden">
            {jobs.map((job) => (
              <JobCard key={job.job_id} job={job} />
            ))}
          </ul>
        </>
      )}

      <div className="mt-4">
        <Pagination
          currentPage={currentPage}
          pages={pages}
          total={total}
          pageSize={currentSize}
          itemLabel="jobs"
          onPageChange={(p) => handleFilterChange('page', String(p))}
        />
      </div>
    </div>
  );
}

function JobName({ job }: { job: Job }) {
  const name = jobTitle(job);
  return (
    <div className="min-w-0">
      <Link href={`/dashboard/jobs/${job.job_id}`} className="block truncate font-medium text-fg hover:text-link" title={name}>
        {name}
      </Link>
      <div className="flex min-w-0 items-center gap-1.5 text-xs text-fg-subtle">
        <span className="shrink-0 tabular-nums">#{job.job_id}</span>
        {job.description && (
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="truncate" tabIndex={0}>
                · {job.description}
              </span>
            </TooltipTrigger>
            <TooltipContent className="max-w-sm">{job.description}</TooltipContent>
          </Tooltip>
        )}
      </div>
    </div>
  );
}

/** Status, plus progress while the job is unfinished */
function JobStatusCell({ job }: { job: Job }) {
  const unfinished = job.status === 'IN_PROGRESS' || job.status === 'PAUSED';
  return (
    <div className="flex items-center gap-2">
      <StatusBadge status={job.status} />
      {unfinished && <span className="text-xs tabular-nums text-fg-muted">{Math.floor(job.progress_percentage ?? 0)}%</span>}
    </div>
  );
}

function JobRow({ job, selected, onToggle }: { job: Job; selected: boolean; onToggle: (id: number) => void }) {
  const run = jobRunWindow(job);
  const completed = job.status === 'COMPLETED';
  return (
    <TableRow data-state={selected ? 'selected' : undefined} className={cn('border-border hover:bg-surface-raised', selected && 'bg-primary/5')}>
      <TableCell className="pl-4">
        <Checkbox
          checked={selected}
          disabled={!completed}
          onCheckedChange={() => onToggle(job.job_id)}
          aria-label={completed ? `Select job ${job.job_id} for comparison` : `Job ${job.job_id} is not completed`}
        />
      </TableCell>
      <TableCell className="max-w-64">
        <JobName job={job} />
      </TableCell>
      <TableCell>
        <JobStatusCell job={job} />
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-1">
          <JobTypeBadge jobType={job.job_type} />
          {job.gpu_enabled && <GpuBadge />}
        </div>
      </TableCell>
      <TableCell className="hidden xl:table-cell">
        <AlgorithmBadge algorithm={job.force_algorithm} />
      </TableCell>
      <TableCell>
        <IntegratorBadge method={job.integration_method} />
      </TableCell>
      <TableCell className="hidden text-right tabular-nums text-fg-muted lg:table-cell">{formatCount(job.num_epochs)}</TableCell>
      <TableCell className="hidden text-right tabular-nums text-fg-muted lg:table-cell">{job.time_step}</TableCell>
      <TableCell className="hidden text-right font-mono text-xs text-fg-muted 2xl:table-cell">{formatSofteningSq(job.softening_sq)}</TableCell>
      <TableCell className="text-right tabular-nums text-fg-muted">
        <Duration start={run.start} end={run.end} />
      </TableCell>
      <TableCell className="hidden text-right text-fg-muted lg:table-cell">
        <LocalTime value={job.created_at} format="date" />
      </TableCell>
      <TableCell className="pr-3 text-right">
        <JobActionsMenu job={job} />
      </TableCell>
    </TableRow>
  );
}

function JobCard({ job }: { job: Job }) {
  const run = jobRunWindow(job);
  return (
    <li className="rounded-lg border border-border bg-surface p-3">
      <div className="flex items-start justify-between gap-2">
        <JobName job={job} />
        <JobActionsMenu job={job} />
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <JobStatusCell job={job} />
        <JobTypeBadge jobType={job.job_type} />
        <IntegratorBadge method={job.integration_method} />
      </div>
      <p className="mt-2 text-xs text-fg-subtle">
        <LocalTime value={job.created_at} format="date" /> · <Duration start={run.start} end={run.end} /> ·{' '}
        {formatCount(job.num_epochs)} epochs
      </p>
    </li>
  );
}
