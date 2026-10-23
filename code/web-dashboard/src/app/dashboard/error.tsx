// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { startTransition, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { AlertTriangle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

export default function DashboardError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const router = useRouter();
  useEffect(() => {
    console.error('Dashboard error:', error);
  }, [error]);

  // reset() alone re-renders with the data that failed: refetch it first
  const retry = () =>
    startTransition(() => {
      router.refresh();
      reset();
    });

  return (
    <div className="flex items-center justify-center p-4 min-h-[50vh]">
      <Card className="w-full max-w-md surface-card border-danger/30">
        <CardHeader className="text-center">
          <AlertTriangle className="h-12 w-12 text-danger mx-auto mb-4" />
          <CardTitle className="text-fg">Something went wrong</CardTitle>
          <CardDescription className="text-fg-muted">
            {/* Server errors reach the browser with a generic message in production */}
            {error.message || 'An unexpected error occurred while loading this page.'}
          </CardDescription>
        </CardHeader>
        <CardContent className="text-center">
          <Button onClick={retry} variant="outline">
            Try again
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
