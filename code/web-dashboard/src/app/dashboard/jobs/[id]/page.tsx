// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import Link from 'next/link';
import { notFound } from 'next/navigation';
import { Download, Film } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { AutoRefresh } from '@/components/AutoRefresh';
import { DemoNote } from '@/components/demo/DemoMode';
import { JobMetricsPanel } from '@/components/comparison/JobMetricsPanel';
import { JobActionsMenu } from '@/components/jobs/JobActionsMenu';
import { JobDescriptionEditor } from '@/components/jobs/JobDescriptionEditor';
import { JobTabs, type JobTab } from '@/components/jobs/JobTabs';
import {
  AlgorithmBadge,
  GpuBadge,
  IntegratorBadge,
  JobTypeBadge,
  PartitionBadge,
  StatusBadge,
} from '@/components/jobs/badges';
import { PageHeader } from '@/components/layout/PageHeader';
import { RefreshControls } from '@/components/list/RefreshControls';
import { Duration, LocalTime } from '@/components/LocalTime';
import { APIServerError, getJob, getJobPackages, getJobProgress } from '@/lib/data/server';
import { capabilities } from '@/lib/capabilities';
import { canViewAnimationFor } from '@/lib/domain/job-status';
import { resultsDownloadUrl } from '@/lib/demo';
import { formatSofteningSq } from '@/lib/format-softening';
import { formatCount, formatFileSize, jobRunWindow } from '@/lib/utils';
import type { Job, JobPackage } from '@/types/api';
import { JobNameEditor } from '@/components/jobs/JobNameEditor';

// No `dynamic = 'force-dynamic'`: the data layer reads cookies(), which already
// makes this page dynamic, and the static demo build must be able to prerender it.

export default async function JobDetailsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id: jobId } = await params;

  // Only a missing job (or a malformed id) is "not found". Anything else reaches the
  // error boundary instead of masquerading as a 404 (a 401 already redirected to /logout)
  let job: Job;
  try {
    job = await getJob(jobId);
  } catch (error) {
    if (error instanceof APIServerError && (error.status === 404 || error.status === 400)) notFound();
    throw error;
  }

  // Progress and (V1 distributed) packages in parallel. Neither is essential
  const [progress, packages] = await Promise.all([
    getJobProgress(jobId).catch(() => null),
    job.job_type === 'distributed' ? getJobPackages(jobId).catch(() => [] as JobPackage[]) : Promise.resolve([] as JobPackage[]),
  ]);

  const active = job.status === 'PENDING' || job.status === 'IN_PROGRESS';
  const completed = job.status === 'COMPLETED';
  const canViewAnimation = canViewAnimationFor(job);

  const tabs: JobTab[] = [
    {
      id: 'overview',
      label: 'Overview',
      content: (
        <OverviewTab
          job={job}
          currentEpoch={progress?.current_epoch ?? job.current_epoch}
          progressPercent={progress?.progress_percentage ?? job.progress_percentage ?? 0}
        />
      ),
    },
    { id: 'parameters', label: 'Parameters', content: <ParametersTab job={job} /> },
  ];
  if (completed) tabs.push({ id: 'conservation', label: 'Conservation', content: <JobMetricsPanel jobId={job.job_id} /> });
  if (packages.length > 0) tabs.push({ id: 'packages', label: 'Packages', content: <PackagesTab packages={packages} /> });

  return (
    <div>
      {/* Re-fetch the server data while the job can still change */}
      <AutoRefresh active={capabilities.liveData && active} />

      <PageHeader
        back={{ href: '/dashboard/jobs', label: 'Jobs' }}
        title={<JobNameEditor key={job.job_id} jobId={job.job_id} name={job.name} filename={job.filename} />}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <span className="tabular-nums">#{job.job_id}</span>
            <StatusBadge status={job.status} />
            <JobTypeBadge jobType={job.job_type} />
            {job.gpu_enabled && <GpuBadge />}
          </span>
        }
        actions={
          <>
            <RefreshControls label="Refresh job" />
            {canViewAnimation && (
              <Button asChild variant="outline">
                <Link href={`/dashboard/jobs/${job.job_id}/animation`}>
                  <Film /> Animation
                </Link>
              </Button>
            )}
            {completed && (
              // A plain link: the browser streams the file from the /api proxy (or the
              // demo's static excerpt). Content-Disposition names it
              <Button asChild>
                <a href={resultsDownloadUrl(job.job_id, job.job_type)} download>
                  <Download /> Results
                </a>
              </Button>
            )}
            <JobActionsMenu job={job} onDetailPage />
          </>
        }
      />
      {completed && (
        <div className="-mt-4 mb-4">
          <DemoNote>The demo download is an excerpt of the results at the snapshot epochs.</DemoNote>
        </div>
      )}

      <JobTabs tabs={tabs} />
    </div>
  );
}

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md bg-surface-raised px-3 py-2.5">
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd className="mt-0.5 truncate font-medium text-fg tabular-nums">{children}</dd>
    </div>
  );
}

function OverviewTab({ job, currentEpoch, progressPercent }: { job: Job; currentEpoch: number; progressPercent: number }) {
  const run = jobRunWindow(job);
  const unfinished = job.status !== 'COMPLETED';
  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <section className="space-y-5 rounded-lg border border-border bg-surface p-5 lg:col-span-2">
        <div>
          <div className="mb-2 flex items-baseline justify-between gap-4">
            <h2 className="text-sm font-medium text-fg">Progress</h2>
            <span className="text-sm tabular-nums text-fg-muted">
              {formatCount(currentEpoch)} / {formatCount(job.num_epochs)} epochs
              {unfinished && ` · ${progressPercent.toFixed(1)}%`}
            </span>
          </div>
          <Progress value={job.num_epochs > 0 ? Math.min(100, (currentEpoch / job.num_epochs) * 100) : 0} className="h-2 bg-surface-raised" />
        </div>
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <Stat label="Duration">
            <Duration start={run.start} end={run.end} fallback={job.status === 'PENDING' ? 'Not started' : 'Unknown'} />
          </Stat>
          <Stat label="Created">
            <LocalTime value={job.created_at} />
          </Stat>
          <Stat label="Finished">
            <LocalTime value={job.completed_at} fallback="—" />
          </Stat>
          <Stat label="Input file">
            <span title={job.filename}>{job.filename || 'Unknown'}</span>
          </Stat>
          <Stat label="File size">{job.file_size ? formatFileSize(job.file_size) : 'Unknown'}</Stat>
          <Stat label="Recoveries">{job.recovery_count ?? 0}</Stat>
        </dl>
      </section>
      <section className="rounded-lg border border-border bg-surface p-5">
        <h2 className="mb-3 text-sm font-medium text-fg">Description</h2>
        {/* key: App Router keeps client state across param-only navigation, which
            made job A's description show up (and save) on job B */}
        <JobDescriptionEditor key={job.job_id} jobId={job.job_id} initialDescription={job.description} />
      </section>
    </div>
  );
}

type Row = [string, React.ReactNode];

function ParametersTab({ job }: { job: Job }) {
  const distributed = job.job_type === 'distributed' || job.job_type === 'distributed_v2';
  const rows: Row[] = [
    ['Job type', <JobTypeBadge key="t" jobType={job.job_type} />],
    ['Epochs', formatCount(job.num_epochs)],
    ['Time step (dt)', job.time_step],
    ['Snapshot interval', `every ${formatCount(job.snapshot_interval)} epochs`],
    ['Force algorithm', <AlgorithmBadge key="a" algorithm={job.force_algorithm} long />],
  ];
  if (job.force_algorithm === 'barnes_hut') rows.push(['Opening angle (θ)', job.barnes_hut_theta ?? 0.5]);
  rows.push(
    ['Integrator', <IntegratorBadge key="i" method={job.integration_method} />],
    ['Softening (ε²)', <span key="s" className="font-mono">{formatSofteningSq(job.softening_sq)}</span>],
    ['Adaptive time step', job.adaptive_enabled ? 'On' : 'Off']
  );
  if (distributed) rows.push(['Package size', `${formatCount(job.package_size)} bodies`]);
  if (job.job_type === 'distributed_v2') {
    rows.push([
      'Partitioning',
      <span key="p" className="flex items-center gap-1.5">
        <PartitionBadge strategy={job.partition_strategy} />
        {job.partition_strategy === 'random' && job.partition_seed != null && (
          <span className="text-xs text-fg-muted">seed {job.partition_seed}</span>
        )}
      </span>,
    ]);
  }
  rows.push(['GPU', job.gpu_enabled ? 'Yes' : 'No']);

  return (
    <section className="rounded-lg border border-border bg-surface">
      <dl className="divide-y divide-border">
        {rows.map(([label, value]) => (
          <div key={label} className="grid grid-cols-2 items-center gap-4 px-5 py-3 sm:grid-cols-3">
            <dt className="text-sm text-fg-muted">{label}</dt>
            <dd className="text-sm text-fg tabular-nums sm:col-span-2">{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function PackagesTab({ packages }: { packages: JobPackage[] }) {
  const count = (statuses: string[]) => packages.filter((p) => statuses.includes(p.status)).length;
  const groups = [
    { label: 'Merged', value: count(['MERGED']), className: 'text-success' },
    { label: 'In progress', value: count(['IN_PROGRESS', 'TO_MERGE']), className: 'text-info' },
    { label: 'Pending', value: count(['PENDING']), className: 'text-caution' },
    { label: 'Failed', value: count(['FAILED']), className: 'text-danger' },
  ];
  return (
    <section className="rounded-lg border border-border bg-surface p-5">
      <h2 className="mb-3 text-sm font-medium text-fg">Work packages of the current epoch ({packages.length})</h2>
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {groups.map((g) => (
          <div key={g.label} className="rounded-md bg-surface-raised px-3 py-2.5">
            <dt className="text-xs text-fg-muted">{g.label}</dt>
            <dd className={`text-lg font-semibold tabular-nums ${g.className}`}>{g.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
