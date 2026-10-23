// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useState, useTransition } from 'react';
import { useRouter } from 'next/navigation';
import { RefreshCw, Timer } from 'lucide-react';
import { AutoRefresh } from '@/components/AutoRefresh';
import { Button } from '@/components/ui/button';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';

/**
 * Re-fetch a server-rendered page: a refresh button whose spinner lasts as long
 * as the refresh really takes (useTransition), and optionally an auto-refresh
 * toggle.
 */
export function RefreshControls({ label, autoRefresh = false, intervalMs = 30000 }: { label: string; autoRefresh?: boolean; intervalMs?: number }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [auto, setAuto] = useState(false);

  return (
    <div className="flex items-center gap-1">
      {autoRefresh && (
        <>
          <AutoRefresh active={auto} intervalMs={intervalMs} />
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setAuto((a) => !a)}
                aria-pressed={auto}
                className={cn('text-fg-muted hover:text-fg', auto && 'bg-primary/15 text-fg')}
              >
                <Timer />
                Auto
              </Button>
            </TooltipTrigger>
            <TooltipContent>Refresh every {Math.round(intervalMs / 1000)} s</TooltipContent>
          </Tooltip>
        </>
      )}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => startTransition(() => router.refresh())}
            disabled={pending}
            aria-label={label}
            className="text-fg-muted hover:text-fg"
          >
            <RefreshCw className={cn(pending && 'animate-spin')} />
          </Button>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    </div>
  );
}
