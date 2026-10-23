// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { use } from 'react';
import dynamic from 'next/dynamic';
import { parseJobId } from '@/lib/utils';

// Three.js / React Three Fiber cannot be server-rendered at all.
// Even with 'use client', Next.js pre-renders on the server as HTML.
// dynamic + ssr:false ensures the component only loads in the browser.
const AnimationViewer = dynamic(
  () => import('@/components/physics/AnimationViewer').then(mod => mod.AnimationViewer),
  { ssr: false, loading: () => (
    <div className="flex flex-1 items-center justify-center">
      <p className="text-fg-muted animate-pulse">Loading 3D viewer...</p>
    </div>
  )}
);

export default function AnimationPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const jobId = parseJobId(id);

  if (jobId === null) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-danger">Invalid job ID</p>
      </div>
    );
  }

  return (
    // Fills the rest of the viewport below the mobile header / demo banner (the
    // dashboard's main is a flex column). The viewer is laid out against this box
    <div className="relative -m-4 min-h-112 flex-1 bg-bg sm:-m-6">
      <h1 className="sr-only">Animation of job {jobId}</h1>
      <div className="absolute inset-0">
        <AnimationViewer key={jobId} jobId={jobId} snapshotsPerPage={30} />
      </div>
    </div>
  );
}
