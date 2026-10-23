// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { startTransition } from 'react';
import { useRouter } from 'next/navigation';
import './globals.css';

// Last-resort boundary for errors in the root layout itself (the dashboard's own
// error.tsx handles its pages). It replaces the root layout, so it brings its
// own <html> and <body>.
export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  const router = useRouter();
  // reset() alone re-renders with the data that failed: refetch it first
  const retry = () =>
    startTransition(() => {
      router.refresh();
      reset();
    });
  return (
    <html lang="en" className="dark">
      <body className="flex min-h-screen items-center justify-center bg-bg p-4 font-sans text-fg">
        <div className="w-full max-w-md rounded-lg border border-danger/30 bg-surface p-6 text-center">
          <h1 className="mb-2 text-lg font-semibold">The dashboard failed to load</h1>
          <p className="mb-4 text-sm text-fg-muted">{error.message || 'An unexpected error occurred.'}</p>
          <button type="button" onClick={retry} className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-fg hover:bg-primary-hover">
            Try again
          </button>
        </div>
      </body>
    </html>
  );
}
