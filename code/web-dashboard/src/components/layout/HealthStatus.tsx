// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { RefreshCw, Wifi, WifiOff } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { useHealth } from '@/hooks/use-api';
import { capabilities } from '@/lib/capabilities';
import { cn } from '@/lib/utils';

/** Backend reachability, with a button to check again */
export function HealthStatus() {
  const { isLoading, isError, isFetching, refetch } = useHealth();
  const label = isLoading ? 'Checking…' : isError ? 'Backend offline' : capabilities.dataSourceLabel;
  const Icon = isError ? WifiOff : Wifi;

  return (
    <div className="flex items-center justify-between rounded-md border border-border bg-surface px-3 py-2" role="status">
      <span className="flex items-center gap-2 text-sm text-fg-muted">
        <Icon className={cn('size-4', isLoading ? 'text-fg-subtle' : isError ? 'text-danger' : 'text-success')} aria-hidden />
        {label}
      </span>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            size="icon-xs"
            variant="ghost"
            onClick={() => refetch()}
            disabled={isFetching}
            aria-label="Check the connection again"
            className="text-fg-muted hover:text-fg"
          >
            <RefreshCw className={cn(isFetching && 'animate-spin')} />
          </Button>
        </TooltipTrigger>
        <TooltipContent>Check the connection again</TooltipContent>
      </Tooltip>
    </div>
  );
}
