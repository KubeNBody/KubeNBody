// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { Suspense } from 'react';
import { PageHeader } from '@/components/layout/PageHeader';
import { SubmitJobForm } from '@/components/submit/SubmitJobForm';

export default function SubmitJobPage() {
  return (
    <div>
      <PageHeader title="Submit a job" description="Choose the input bodies and the simulation parameters" />
      {/* The form reads ?file= (useSearchParams), which needs a Suspense boundary in a static page */}
      <Suspense>
        <SubmitJobForm />
      </Suspense>
    </div>
  );
}
