// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { Suspense, useEffect, useMemo, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { Check, GitCompare, Plus, Printer, Save, Star, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command';
import { LoadingSpinner } from '@/components/ui/loading-spinner';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ComparisonResultsView } from '@/components/comparison';
import { JobMetricsPanel } from '@/components/comparison/JobMetricsPanel';
import { MetricsSummaryTable } from '@/components/comparison/MetricsSummaryTable';
import { AlgorithmBadge, IntegratorBadge, JobTypeBadge } from '@/components/jobs/badges';
import { PageHeader } from '@/components/layout/PageHeader';
import { useCreateComparisonJob, useMultipleJobSummaries, useSelectableJobs } from '@/hooks/use-api';
import { apiClient } from '@/lib/api-client';
import { capabilities } from '@/lib/capabilities';
import { formatSofteningSq } from '@/lib/format-softening';
import { integrationMethodLabel } from '@/lib/format-integration';
import { formatCount } from '@/lib/utils';
import type { JobComparisonSummary } from '@/types/api';
import { jobTitle } from '@/lib/domain/job-title';

// Backend limits (src/api_schemas.py, routes/comparison.py): POST /compare takes at
// most 5 comparison jobs, a saved comparison 10, and the summaries query 11 ids
// (the reference and 10 comparison jobs)
const MAX_RUN_COMPARISON_JOBS = 5;
const MAX_SAVED_COMPARISON_JOBS = 10;
const MAX_SELECTED_JOBS = MAX_SAVED_COMPARISON_JOBS + 1;

/** The selection in the URL: ?ref=41&jobs=42,43 (the first id is the reference) */
function parseSelection(params: URLSearchParams): number[] {
  const ids = [params.get('ref'), ...(params.get('jobs') ?? '').split(',')]
    .map((v) => Number(v))
    .filter((n) => Number.isInteger(n) && n > 0);
  return Array.from(new Set(ids)).slice(0, MAX_SELECTED_JOBS);
}

export default function ComparePage() {
  return (
    // useSearchParams needs a Suspense boundary in a statically rendered page
    <Suspense>
      <Compare />
    </Suspense>
  );
}

function Compare() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const jobIds = useMemo(() => parseSelection(searchParams), [searchParams]);
  const [referenceId, ...comparisonIds] = jobIds;

  const setSelection = (ids: number[]) => {
    const [ref, ...rest] = ids;
    const qs = ref ? `?ref=${ref}${rest.length ? `&jobs=${rest.join(',')}` : ''}` : '';
    router.replace(`${pathname}${qs}`, { scroll: false });
  };

  const { data: summaries } = useMultipleJobSummaries(jobIds);
  const byId = useMemo(() => new Map((summaries ?? []).map((s) => [s.job_id, s])), [summaries]);

  // Report timestamp for the printed header, taken when printing
  const [printedAt, setPrintedAt] = useState<string | null>(null);
  useEffect(() => {
    const onBefore = () => setPrintedAt(new Date().toLocaleString());
    window.addEventListener('beforeprint', onBefore);
    return () => window.removeEventListener('beforeprint', onBefore);
  }, []);

  return (
    <div>
      <div className="no-print">
        <PageHeader
          title="Compare runs"
          description="Conservation of each run, and how far each one drifts from a reference run"
          actions={
            jobIds.length > 0 && (
              <Button variant="outline" onClick={() => window.print()}>
                <Printer /> Print / PDF
              </Button>
            )
          }
        />
      </div>
      <div className="mb-4 hidden print:block">
        <h1 className="text-xl font-bold">N-body comparison report</h1>
        <p className="text-sm">
          Generated {printedAt} · jobs {jobIds.map((id) => `#${id}`).join(', ')}
        </p>
      </div>

      <SelectionPanel jobIds={jobIds} byId={byId} onChange={setSelection} />

      {jobIds.length > 0 && (
        <Tabs defaultValue={jobIds.length > 1 ? 'comparison' : 'conservation'} className="mt-6">
          <TabsList className="no-print mb-4 bg-surface">
            <TabsTrigger value="comparison" disabled={jobIds.length < 2} className="text-fg-muted data-[state=active]:bg-surface-raised data-[state=active]:text-fg">
              Against the reference
            </TabsTrigger>
            <TabsTrigger value="conservation" className="text-fg-muted data-[state=active]:bg-surface-raised data-[state=active]:text-fg">
              Conservation per run
            </TabsTrigger>
          </TabsList>
          <TabsContent value="comparison">
            {jobIds.length >= 2 && <ComparisonRunner referenceId={referenceId} comparisonIds={comparisonIds} />}
          </TabsContent>
          <TabsContent value="conservation" className="space-y-6">
            <MetricsSummaryTable jobIds={jobIds} />
            <PerJobMetrics jobIds={jobIds} />
          </TabsContent>
        </Tabs>
      )}
    </div>
  );
}

function SelectionPanel({ jobIds, byId, onChange }: { jobIds: number[]; byId: Map<number, JobComparisonSummary>; onChange: (ids: number[]) => void }) {
  const full = jobIds.length >= MAX_SELECTED_JOBS;
  return (
    <section className="no-print space-y-3 rounded-lg border border-border bg-surface p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-fg">Jobs to compare</h2>
          <p className="text-xs text-fg-muted">
            Pick completed jobs. The first one is the reference, the others are compared against it.{' '}
            {capabilities.canMutate
              ? `Compare here with up to ${MAX_RUN_COMPARISON_JOBS} others, or save and run in the background with up to ${MAX_SAVED_COMPARISON_JOBS}.`
              : `Up to ${MAX_RUN_COMPARISON_JOBS} others.`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {jobIds.length > 0 && (
            <Button variant="ghost" size="sm" onClick={() => onChange([])} className="text-fg-muted hover:text-fg">
              Clear
            </Button>
          )}
          <JobPicker selected={jobIds} disabled={full} onAdd={(id) => onChange([...jobIds, id])} />
        </div>
      </div>

      {jobIds.length === 0 ? (
        <p className="rounded-md border border-dashed border-border-strong px-4 py-8 text-center text-sm text-fg-muted">
          Add completed jobs to compare them, or select them in the jobs list and choose “Compare”.
        </p>
      ) : (
        <div className="rounded-md border border-border">
          <Table>
            <TableHeader>
              <TableRow className="border-border hover:bg-transparent">
                <TableHead className="pl-3 text-fg-muted">Job</TableHead>
                <TableHead className="hidden text-fg-muted md:table-cell">Type</TableHead>
                <TableHead className="text-fg-muted">Algorithm</TableHead>
                <TableHead className="text-fg-muted">Integrator</TableHead>
                <TableHead className="hidden text-right text-fg-muted lg:table-cell">dt</TableHead>
                <TableHead className="hidden text-right text-fg-muted lg:table-cell">ε²</TableHead>
                <TableHead className="hidden text-right text-fg-muted lg:table-cell">Epochs</TableHead>
                <TableHead className="w-32 pr-3">
                  <span className="sr-only">Role</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {jobIds.map((id, i) => {
                const s = byId.get(id);
                return (
                  <TableRow key={id} className="border-border hover:bg-surface-raised">
                    <TableCell className="max-w-56 pl-3">
                      <p className="truncate font-medium text-fg" title={s ? jobTitle(s) : undefined}>
                        {s ? jobTitle(s) : `Job ${id}`}
                      </p>
                      <p className="text-xs tabular-nums text-fg-subtle">#{id}</p>
                    </TableCell>
                    <TableCell className="hidden md:table-cell">{s && <JobTypeBadge jobType={s.job_type} />}</TableCell>
                    <TableCell>
                      {s && <AlgorithmBadge algorithm={s.force_algorithm} />}
                      {s?.force_algorithm === 'barnes_hut' && s.barnes_hut_theta != null && <span className="ml-1 text-xs text-fg-muted">θ={s.barnes_hut_theta}</span>}
                    </TableCell>
                    <TableCell>{s && <IntegratorBadge method={s.integration_method} />}</TableCell>
                    <TableCell className="hidden text-right tabular-nums text-fg-muted lg:table-cell">{s?.time_step}</TableCell>
                    <TableCell className="hidden text-right font-mono text-xs text-fg-muted lg:table-cell">{s && formatSofteningSq(s.softening_sq)}</TableCell>
                    <TableCell className="hidden text-right tabular-nums text-fg-muted lg:table-cell">{s && formatCount(s.num_epochs)}</TableCell>
                    <TableCell className="pr-3">
                      <div className="flex items-center justify-end gap-1">
                        {i === 0 ? (
                          <span className="inline-flex items-center gap-1 rounded border border-primary/50 bg-primary/15 px-2 py-0.5 text-xs font-medium text-fg">
                            <Star className="size-3 fill-current text-link" aria-hidden /> Reference
                          </span>
                        ) : (
                          <Button variant="ghost" size="xs" onClick={() => onChange([id, ...jobIds.filter((x) => x !== id)])} className="text-fg-muted hover:text-fg">
                            Make reference
                          </Button>
                        )}
                        <Button variant="ghost" size="icon-xs" onClick={() => onChange(jobIds.filter((x) => x !== id))} aria-label={`Remove job ${id}`} className="text-fg-muted hover:text-danger">
                          <X />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </section>
  );
}

/** Searchable list of completed jobs to add (the old plain Select listed every job unfiltered) */
function JobPicker({ selected, disabled, onAdd }: { selected: number[]; disabled: boolean; onAdd: (id: number) => void }) {
  const [open, setOpen] = useState(false);
  const { data: jobs, isLoading } = useSelectableJobs('COMPLETED', { staleTime: 60 * 1000 });
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button size="sm" disabled={disabled}>
          <Plus /> {disabled ? `At most ${MAX_SELECTED_JOBS} runs` : 'Add run'}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[min(32rem,90vw)] border-border bg-surface-raised p-0">
        <Command filter={(value, search) => (value.toLowerCase().includes(search.trim().toLowerCase()) ? 1 : 0)}>
          <CommandInput placeholder="Search name, #id, integrator…" />
          <CommandList>
            <CommandEmpty>{isLoading ? 'Loading…' : 'No completed job matches.'}</CommandEmpty>
            <CommandGroup>
              {(jobs ?? []).map((j) => {
                const taken = selected.includes(j.job_id);
                return (
                  <CommandItem
                    key={j.job_id}
                    value={`#${j.job_id} ${j.name ?? ''} ${j.filename ?? ''} ${j.force_algorithm} ${j.integration_method} ${integrationMethodLabel(j.integration_method)} ${j.description ?? ''}`}
                    disabled={taken}
                    onSelect={() => {
                      onAdd(j.job_id);
                      setOpen(false);
                    }}
                  >
                    <div className="min-w-0">
                      <p className="truncate">
                        <span className="tabular-nums text-fg-subtle">#{j.job_id}</span> {jobTitle(j)}
                      </p>
                      <p className="truncate text-xs text-fg-muted">
                        {integrationMethodLabel(j.integration_method)} · {j.force_algorithm === 'barnes_hut' ? 'Barnes–Hut' : 'Direct'} · dt {j.time_step} · ε²{' '}
                        {formatSofteningSq(j.softening_sq)}
                      </p>
                    </div>
                    {taken && <Check className="ml-auto text-success" />}
                  </CommandItem>
                );
              })}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

/**
 * Runs the comparison on request. The result is a query keyed on the selection,
 * so changing the selection simply shows nothing until it is run again, with no
 * stale result racing in.
 */
function ComparisonRunner({ referenceId, comparisonIds }: { referenceId: number; comparisonIds: number[] }) {
  const router = useRouter();
  const key = [referenceId, ...comparisonIds].join(',');
  const [requested, setRequested] = useState<string | null>(null);
  const comparison = useQuery({
    queryKey: ['comparison', 'run', key],
    queryFn: () => apiClient.compareJobs(referenceId, comparisonIds),
    enabled: requested === key,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
  const save = useCreateComparisonJob();
  const tooManyToRun = comparisonIds.length > MAX_RUN_COMPARISON_JOBS;
  const tooManyToSave = comparisonIds.length > MAX_SAVED_COMPARISON_JOBS;
  const shown = requested === key ? comparison : null;

  return (
    <div className="space-y-6">
      <div className="no-print flex flex-wrap items-center gap-3 rounded-lg border border-border bg-surface p-4">
        <p className="mr-auto text-sm text-fg-muted">
          #{referenceId} against {comparisonIds.map((id) => `#${id}`).join(', ')}
        </p>
        <Button onClick={() => setRequested(key)} disabled={tooManyToRun || (shown?.isFetching ?? false)}>
          {shown?.isFetching ? <LoadingSpinner size="sm" /> : <GitCompare />}
          {shown?.isFetching ? 'Comparing…' : 'Run comparison'}
        </Button>
        {capabilities.canMutate && (
          <Button
            variant="outline"
            disabled={tooManyToSave || save.isPending}
            onClick={() =>
              save.mutate(
                { referenceJobId: referenceId, comparisonJobIds: comparisonIds },
                { onSuccess: (data) => router.push(`/dashboard/comparisons/${data.comparison_id}`) }
              )
            }
          >
            {save.isPending ? <LoadingSpinner size="sm" /> : <Save />}
            Save and run in background
          </Button>
        )}
        {(tooManyToRun || tooManyToSave) && (
          <p className="w-full text-sm text-warning">
            {tooManyToSave
              ? `Too many jobs: a comparison takes at most ${MAX_SAVED_COMPARISON_JOBS} besides the reference.`
              : capabilities.canMutate
                ? `Too many to compare here (at most ${MAX_RUN_COMPARISON_JOBS} besides the reference). Remove some, or save the comparison to run it in the background (up to ${MAX_SAVED_COMPARISON_JOBS}).`
                : `Too many jobs: at most ${MAX_RUN_COMPARISON_JOBS} besides the reference.`}
          </p>
        )}
        {save.isError && <p className="w-full text-sm text-danger">Saving failed: {save.error?.message}</p>}
      </div>

      {shown?.isError && (
        <p role="alert" className="rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
          The comparison failed: {shown.error?.message || 'unknown error'}
          {!capabilities.liveData && ' (only recorded comparisons are available here, try another reference)'}
        </p>
      )}
      {shown?.isFetching && !shown.data && (
        <p className="flex items-center justify-center gap-3 py-10 text-sm text-fg-muted" role="status">
          <LoadingSpinner /> Comparing trajectories (large runs take a while)…
        </p>
      )}
      {shown?.data && <ComparisonResultsView results={shown.data} />}
      {!shown && <p className="py-6 text-center text-sm text-fg-muted">Run the comparison to see divergence and conservation against #{referenceId}.</p>}
    </div>
  );
}

/** One run's energy charts at a time (all of them at once made a very long page) */
function PerJobMetrics({ jobIds }: { jobIds: number[] }) {
  const [current, setCurrent] = useState(jobIds[0]);
  const active = jobIds.includes(current) ? current : jobIds[0];
  return (
    <div className="space-y-3">
      {jobIds.length > 1 && (
        <div className="no-print flex flex-wrap gap-1" role="tablist" aria-label="Run">
          {jobIds.map((id) => (
            <Button
              key={id}
              role="tab"
              aria-selected={id === active}
              variant="ghost"
              size="sm"
              onClick={() => setCurrent(id)}
              className={id === active ? 'bg-surface-raised text-fg' : 'text-fg-muted hover:text-fg'}
            >
              #{id}
            </Button>
          ))}
        </div>
      )}
      <JobMetricsPanel key={active} jobId={active} />
    </div>
  );
}
