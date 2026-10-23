// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import Link from 'next/link';
import { ArrowRight, Upload } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { IntegratorBadge, JobTypeBadge, StatusBadge } from '@/components/jobs/badges';
import { Duration, LocalTime } from '@/components/LocalTime';
import { jobRunWindow } from '@/lib/utils';
import type { Job } from '@/types/api';
import { jobTitle } from '@/lib/domain/job-title';

export function RecentJobs({ jobs }: { jobs: Job[] }) {
  return (
    <section aria-labelledby="recent-jobs" className="rounded-lg border border-border bg-surface">
      <div className="flex items-center justify-between px-5 pt-4 pb-2">
        <h2 id="recent-jobs" className="text-sm font-medium text-fg">
          Recent jobs
        </h2>
        <Button asChild variant="ghost" size="sm" className="text-fg-muted hover:text-fg">
          <Link href="/dashboard/jobs">
            All jobs <ArrowRight />
          </Link>
        </Button>
      </div>

      {jobs.length === 0 ? (
        <div className="px-5 pb-8 pt-4 text-center">
          <Upload className="mx-auto mb-3 size-10 text-fg-subtle" aria-hidden />
          <p className="text-fg">No jobs yet</p>
          <p className="mb-4 text-sm text-fg-muted">Submit a simulation to see it here.</p>
          <Button asChild>
            <Link href="/dashboard/submit">Submit a job</Link>
          </Button>
        </div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow className="border-border hover:bg-transparent">
              <TableHead className="pl-5 text-fg-muted">Job</TableHead>
              <TableHead className="pr-5 text-fg-muted sm:pr-2">Status</TableHead>
              <TableHead className="hidden text-fg-muted md:table-cell">Type</TableHead>
              <TableHead className="hidden text-fg-muted lg:table-cell">Integrator</TableHead>
              <TableHead className="hidden text-right text-fg-muted sm:table-cell">Duration</TableHead>
              <TableHead className="hidden pr-5 text-right text-fg-muted sm:table-cell">Created</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {jobs.map((job) => {
              const run = jobRunWindow(job);
              return (
                <TableRow key={job.job_id} className="border-border hover:bg-surface-raised">
                  <TableCell className="max-w-0 pl-5 sm:max-w-xs">
                    <Link
                      href={`/dashboard/jobs/${job.job_id}`}
                      className="block truncate font-medium text-fg hover:text-link"
                      title={jobTitle(job)}
                    >
                      {jobTitle(job)}
                    </Link>
                    <span className="text-xs tabular-nums text-fg-subtle">
                      #{job.job_id}
                      <span className="sm:hidden">
                        {' · '}
                        <LocalTime value={job.created_at} format="date" />
                      </span>
                    </span>
                  </TableCell>
                  <TableCell className="pr-5 sm:pr-2">
                    <StatusBadge status={job.status} />
                  </TableCell>
                  <TableCell className="hidden md:table-cell">
                    <JobTypeBadge jobType={job.job_type} />
                  </TableCell>
                  <TableCell className="hidden lg:table-cell">
                    <IntegratorBadge method={job.integration_method} />
                  </TableCell>
                  <TableCell className="hidden text-right tabular-nums text-fg-muted sm:table-cell">
                    <Duration start={run.start} end={run.end} />
                  </TableCell>
                  <TableCell className="hidden pr-5 text-right text-fg-muted sm:table-cell">
                    <LocalTime value={job.created_at} format="date" />
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
    </section>
  );
}
