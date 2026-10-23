// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Demo build's files list (page.server.tsx filters on the server from the URL,
// a static page can't): prerender every demo input file, filter in the browser (DemoLists).
import { Suspense } from 'react';
import { getFiles } from '@/lib/data/demo/server';
import { DemoFilesList } from '@/components/demo/DemoLists';
import { LoadingSpinner } from '@/components/ui/loading-spinner';

export default async function FilesPage() {
  const { files } = await getFiles({ size: 100 });

  return (
    <Suspense
      fallback={
        <div className="flex justify-center py-20">
          <LoadingSpinner size="lg" />
        </div>
      }
    >
      <DemoFilesList allFiles={files} />
    </Suspense>
  );
}
