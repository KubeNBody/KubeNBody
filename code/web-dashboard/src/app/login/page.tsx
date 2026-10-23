// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useActionState, useState } from 'react';
import { AlertCircle, Info, Orbit } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { FormField, fieldClass } from '@/components/ui/form-field';
import { LoadingSpinner } from '@/components/ui/loading-spinner';
import { login } from '@/actions/auth';
import { capabilities } from '@/lib/capabilities';

export default function LoginPage() {
  // A form action: success navigates away (server redirect, or the demo's
  // client-side one). A failure comes back as state
  const [state, action, pending] = useActionState(login, undefined);
  const demo = capabilities.prefilledLogin;
  // Controlled: React resets an uncontrolled form after an action, which cleared
  // the username after a failed attempt (the password is cleared on purpose)
  const [username, setUsername] = useState(demo?.username ?? '');

  return (
    <main className="flex min-h-screen items-center justify-center bg-bg p-4">
      <div className="w-full max-w-sm rounded-lg border border-border bg-surface p-6">
        <div className="mb-6 text-center">
          <span className="mx-auto mb-3 flex size-10 items-center justify-center rounded-md bg-primary/15 text-link">
            <Orbit className="size-6" aria-hidden />
          </span>
          <h1 className="text-xl font-semibold text-fg">KubeNBody</h1>
          <p className="text-sm text-fg-muted">Sign in to continue</p>
        </div>

        <form action={action} className="space-y-4">
          {demo && (
            <p className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 p-3 text-sm text-warning">
              <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>
                Read-only demo with preloaded results. The account is <strong>{demo.username}</strong> / <strong>{demo.password}</strong>, already
                filled in.
              </span>
            </p>
          )}
          {state?.error && (
            <p role="alert" aria-live="assertive" className="flex items-center gap-2 rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
              <AlertCircle className="size-4 shrink-0" aria-hidden />
              {state.error}
            </p>
          )}
          <FormField id="username" label="Username">
            {(p) => <input {...p} name="username" autoComplete="username" required value={username} onChange={(e) => setUsername(e.target.value)} className={fieldClass} />}
          </FormField>
          <FormField id="password" label="Password">
            {(p) => <input {...p} name="password" type="password" autoComplete="current-password" required defaultValue={demo?.password} className={fieldClass} />}
          </FormField>
          <Button type="submit" className="w-full" disabled={pending}>
            {pending && <LoadingSpinner size="sm" />}
            {pending ? 'Signing in…' : demo ? 'Enter the demo' : 'Sign in'}
          </Button>
        </form>
      </div>
    </main>
  );
}
