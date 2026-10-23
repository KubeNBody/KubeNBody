// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

// Used by the static demo build's jobs/files pages (page.demo.tsx). The normal
// pages filter on the server from the URL's search params. A static page can't,
// so it ships every demo item and these wrappers filter + paginate in the
// browser. The list components' own router.push('?status=…') drives them.
import { useSearchParams } from 'next/navigation';
import type { InputFileInfo, Job } from '@/types/api';
import { JobsList } from '@/components/jobs/JobsList';
import { FilesList } from '@/components/files/FilesList';
import { filterFiles, filterJobs, paginate } from '@/lib/demo/query';
import { parsePageParams } from '@/lib/utils';

export function DemoJobsList({ allJobs }: { allJobs: Job[] }) {
  const sp = useSearchParams();
  const filtered = filterJobs(allJobs, {
    status: sp.get('status') || undefined,
    search: sp.get('search') || undefined,
    job_type: sp.get('job_type') || undefined,
    integration_method: sp.get('integration_method') || undefined,
  });
  const { page: n, size } = parsePageParams(sp.get('page'), sp.get('size'));
  const page = paginate(filtered, n, size);

  return (
    <JobsList jobs={page.items} total={page.total} page={page.page} size={page.page_size} pages={page.pages} />
  );
}

export function DemoFilesList({ allFiles }: { allFiles: InputFileInfo[] }) {
  const sp = useSearchParams();
  const filtered = filterFiles(allFiles, {
    search: sp.get('search') || undefined,
    sort_by: sp.get('sort_by') || 'created_at',
  });
  const { page: n, size } = parsePageParams(sp.get('page'), sp.get('size'));
  const page = paginate(filtered, n, size);

  return (
    <FilesList files={page.items} total={page.total} page={page.page} size={page.page_size} pages={page.pages} />
  );
}
