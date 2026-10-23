// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import Link from 'next/link';
import { Orbit } from 'lucide-react';

/** App name (not a heading: each page has its own h1) */
export function Brand() {
  return (
    <Link href="/dashboard" className="flex items-center gap-2 rounded-md px-1 text-fg">
      <span className="flex size-8 items-center justify-center rounded-md bg-primary/15 text-link">
        <Orbit className="size-5" aria-hidden />
      </span>
      <span className="text-base font-semibold tracking-tight">KubeNBody</span>
    </Link>
  );
}
