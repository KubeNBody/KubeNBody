// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { Info } from 'lucide-react';

// Input file formats the backend accepts (src/file_service.py), collapsed by default
export function FileFormatHelp() {
  return (
    <details className="group rounded-md border border-border bg-surface-raised px-3 py-2 text-sm">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-fg-muted hover:text-fg">
        <Info className="size-4 text-link" aria-hidden />
        File format
      </summary>
      <div className="mt-2 space-y-1.5 text-fg-muted">
        <p>
          <strong className="text-fg">CSV</strong> with a header row and the columns{' '}
          <code className="rounded bg-surface px-1 text-xs text-fg">n, idBody, m, x, y, z, vx, vy, vz</code>
        </p>
        <p>
          <strong className="text-fg">Space-separated</strong> (.txt, .dat): 7 columns{' '}
          <code className="rounded bg-surface px-1 text-xs text-fg">m x y z vx vy vz</code>, n and idBody are added.
        </p>
        <p>Numeric values and positive masses. Scientific notation is fine. Up to 100 MB.</p>
      </div>
    </details>
  );
}
