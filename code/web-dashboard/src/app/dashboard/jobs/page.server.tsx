// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { getJobs } from '@/lib/data/server';
import { JobsList } from '@/components/jobs/JobsList';
import { JOB_STATUSES } from '@/types/api';
import { parsePageParams } from '@/lib/utils';

// Filter values the backend accepts (APIJobStatus, JobType). Anything else is dropped
const STATUSES: readonly string[] = JOB_STATUSES;
const JOB_TYPES: readonly string[] = ['distributed', 'distributed_v2', 'monolithic'];

// Force dynamic rendering - don't pre-render at build time
export const dynamic = 'force-dynamic';

interface PageProps {
  searchParams: Promise<{
    page?: string;
    size?: string;
    status?: string;
    search?: string;
    job_type?: string;
    integration_method?: string;
  }>;
}

export default async function JobsPage({ searchParams }: PageProps) {
  const sp = await searchParams;
  // Parse search params
  const { page, size } = parsePageParams(sp.page, sp.size);
  const status = sp.status && STATUSES.includes(sp.status) ? sp.status : '';
  const search = sp.search || '';
  const job_type = sp.job_type && JOB_TYPES.includes(sp.job_type) ? sp.job_type : '';
  const integration_method = sp.integration_method || '';

  // Server-side data fetching - runs on Next.js server, can access k8s services
  const data = await getJobs({
    page,
    size,
    status: status || undefined,
    search: search || undefined,
    job_type: job_type || undefined,
    integration_method: integration_method || undefined,
  });

  return (
    <JobsList jobs={data.jobs} total={data.total} page={data.page} size={data.size} pages={data.pages} />
  );
}
