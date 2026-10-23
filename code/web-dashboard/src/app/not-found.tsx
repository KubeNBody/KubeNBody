// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import Link from 'next/link';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { AlertCircle } from 'lucide-react';

export default function NotFound() {
  return (
    <div className="min-h-screen bg-bg flex items-center justify-center p-4">
      <Card className="surface-card border-border max-w-md">
        <CardContent className="p-8 text-center">
          <AlertCircle className="h-12 w-12 text-fg-muted mx-auto mb-4" />
          <h2 className="text-xl font-semibold text-fg mb-2">Page not found</h2>
          <p className="text-fg-muted mb-6">This page doesn&apos;t exist.</p>
          <Button asChild variant="outline">
            <Link href="/dashboard/jobs">Back to jobs</Link>
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
