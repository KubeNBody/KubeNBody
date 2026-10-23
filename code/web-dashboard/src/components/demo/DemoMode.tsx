// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Info } from 'lucide-react';
import { IS_DEMO, isDemoAuthenticated } from '@/lib/demo';

/**
 * Client-side stand-in for middleware.ts in the static demo build: without the
 * demo "session" flag, send the visitor to the login page. Pass-through in the
 * normal build, where the middleware checks the auth cookie.
 */
export function DemoAuthGate({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [allowed, setAllowed] = useState(!IS_DEMO);

  useEffect(() => {
    if (!IS_DEMO) return;
    if (isDemoAuthenticated()) {
      setAllowed(true);
    } else {
      router.replace('/login');
    }
  }, [router]);

  return allowed ? <>{children}</> : null;
}

export function DemoBanner() {
  if (!IS_DEMO) return null;
  return (
    <p className="mb-4 sm:mb-6 inline-flex max-w-full items-center gap-2 self-start rounded-full border border-warning/30 bg-warning/10 px-3 py-1 text-xs text-warning">
      <Info className="size-3.5 shrink-0" aria-hidden />
      <span>
        <strong className="font-semibold">Read-only demo</strong> with preloaded results: submitting, editing and
        deleting are disabled.
      </span>
    </p>
  );
}

/** Inline note shown next to controls that behave differently in the demo. */
export function DemoNote({ children }: { children: React.ReactNode }) {
  if (!IS_DEMO) return null;
  return <p className="text-xs text-warning">{children}</p>;
}
