// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useCallback, useState, useTransition } from 'react';
import { useRouter } from 'next/navigation';
import { toast } from 'sonner';
import type { MutationResult } from '@/lib/data/types';

/**
 * Run a mutation (a server action, or its demo stand-in) with one pending
 * flag, a toast for the outcome, and a refresh of the server-rendered page on
 * success. The server action already revalidates its pages, and router.refresh()
 * makes the current client view pick that up.
 */
export function useServerAction() {
  const router = useRouter();
  const [running, setRunning] = useState<string | null>(null);
  const [, startTransition] = useTransition();

  const run = useCallback(
    async (key: string, action: () => Promise<MutationResult>, messages: { success?: string; error: string }) => {
      setRunning(key);
      try {
        const result = await action();
        if (result.success) {
          if (messages.success) toast.success(messages.success);
          startTransition(() => router.refresh());
          return true;
        }
        toast.error(messages.error, { description: result.error });
        return false;
      } catch (error) {
        toast.error(messages.error, { description: error instanceof Error ? error.message : 'Network error' });
        return false;
      } finally {
        setRunning(null);
      }
    },
    [router]
  );

  return { run, running };
}
