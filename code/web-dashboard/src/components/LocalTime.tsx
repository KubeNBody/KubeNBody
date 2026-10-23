// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useEffect, useState } from 'react';
import { formatDuration, parseApiDate } from '@/lib/utils';

type TimeFormat = 'date' | 'datetime' | 'time';

function useMounted() {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  return mounted;
}

// Deterministic UTC text for the server render and the first client render
function formatUtc(date: Date, format: TimeFormat): string {
  const iso = date.toISOString();
  if (format === 'date') return iso.slice(0, 10);
  if (format === 'time') return `${iso.slice(11, 19)} UTC`;
  return `${iso.slice(0, 10)} ${iso.slice(11, 19)} UTC`;
}

function formatLocal(date: Date, format: TimeFormat): string {
  if (format === 'date') return date.toLocaleDateString();
  if (format === 'time') return date.toLocaleTimeString();
  return date.toLocaleString();
}

/**
 * A backend timestamp in the viewer's local time zone. The server (and the
 * hydrating client) render UTC, the local text replaces it after mount, so the
 * pod's time zone never shows and hydration never mismatches.
 */
export function LocalTime({
  value,
  format = 'datetime',
  fallback = 'Unknown',
}: {
  value: string | null | undefined;
  format?: TimeFormat;
  fallback?: string;
}) {
  const mounted = useMounted();
  const date = parseApiDate(value);
  if (!date) return <>{fallback}</>;
  return (
    <time dateTime={date.toISOString()} title={date.toISOString()}>
      {mounted ? formatLocal(date, format) : formatUtc(date, format)}
    </time>
  );
}

/**
 * Elapsed time between two backend timestamps. With no end it runs to now,
 * which is only known on the client (placeholder until mounted).
 */
export function Duration({
  start,
  end,
  fallback = '—',
}: {
  start: string | null | undefined;
  end?: string | null;
  fallback?: string;
}) {
  const mounted = useMounted();
  const startDate = parseApiDate(start);
  const endDate = parseApiDate(end);
  if (!startDate) return <>{fallback}</>;
  if (endDate) return <>{formatDuration(endDate.getTime() - startDate.getTime())}</>;
  return <>{mounted ? formatDuration(Date.now() - startDate.getTime()) : fallback}</>;
}
