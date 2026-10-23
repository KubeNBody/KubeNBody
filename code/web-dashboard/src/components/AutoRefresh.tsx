// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

// Re-runs the current route's server components every `intervalMs` while `active`,
// skipping ticks while the tab is hidden.
export function AutoRefresh({ active, intervalMs = 5000 }: { active: boolean; intervalMs?: number }) {
  const router = useRouter();

  useEffect(() => {
    if (!active) return;
    const interval = setInterval(() => {
      if (!document.hidden) router.refresh();
    }, intervalMs);
    return () => clearInterval(interval);
  }, [active, intervalMs, router]);

  return null;
}
