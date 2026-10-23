// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import Link from 'next/link';
import { Upload } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { PageHeader } from '@/components/layout/PageHeader';
import { RecentComparisons } from '@/components/overview/RecentComparisons';
import { RecentJobs } from '@/components/overview/RecentJobs';
import { StatusSummary } from '@/components/overview/StatusSummary';
import { getJobs } from '@/lib/data/server';

// No `dynamic = 'force-dynamic'`: the data layer reads cookies(), which already
// makes this page dynamic, and the static demo build must be able to prerender it.

export default async function DashboardPage() {
  // The backend caps page_size at 100, so per-status counts cover the newest 100 jobs
  // only. Exact counts beyond that would need a backend aggregate endpoint.
  const { jobs, total } = await getJobs({ size: 100 });

  const statusCounts = jobs.reduce<Record<string, number>>((acc, job) => {
    acc[job.status] = (acc[job.status] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div>
      <PageHeader
        title="Overview"
        description="N-body simulation jobs and their analysis"
        actions={
          <Button asChild>
            <Link href="/dashboard/submit">
              <Upload />
              Submit job
            </Link>
          </Button>
        }
      />

      <StatusSummary counts={statusCounts} total={total} sampled={jobs.length} />


      <div className="mt-6 grid gap-6 xl:grid-cols-3">
        <div className="min-w-0 xl:col-span-2">
          <RecentJobs jobs={jobs.slice(0, 8)} />
        </div>
        <RecentComparisons />
      </div>
    </div>
  );
}
