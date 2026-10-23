// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Demo build's jobs list (page.server.tsx filters on the server from the URL,
// a static page can't): prerender every demo job, filter in the browser (DemoLists).
import { Suspense } from 'react';
import { getJobs } from '@/lib/data/demo/server';
import { DemoJobsList } from '@/components/demo/DemoLists';
import { LoadingSpinner } from '@/components/ui/loading-spinner';

export default async function JobsPage() {
  const { jobs } = await getJobs({ size: 100 });

  return (
    <Suspense
      fallback={
        <div className="flex justify-center py-20">
          <LoadingSpinner size="lg" />
        </div>
      }
    >
      <DemoJobsList allJobs={jobs} />
    </Suspense>
  );
}
