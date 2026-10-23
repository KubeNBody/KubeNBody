// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { type ClassValue, clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// Format file size
export function formatFileSize(bytes: number): string {
  const sizes = ['B', 'KB', 'MB', 'GB'];
  if (bytes === 0) return '0 B';

  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), sizes.length - 1);
  return `${(bytes / Math.pow(1024, i)).toFixed(2)} ${sizes[i]}`;
}

/**
 * Parse a backend timestamp. The API serializes naive UTC datetimes without a
 * zone suffix, which `new Date()` would read as local time, so mark them UTC.
 */
export function parseApiDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(value);
  const date = new Date(hasZone ? value : `${value}Z`);
  return isNaN(date.getTime()) ? null : date;
}

// Format a millisecond duration as "1h 2m 3s" / "2m 3s"
export function formatDuration(ms: number): string {
  const duration = Math.max(0, ms);
  const hours = Math.floor(duration / 3600000);
  const minutes = Math.floor((duration % 3600000) / 60000);
  const seconds = Math.floor((duration % 60000) / 1000);
  return hours > 0 ? `${hours}h ${minutes}m ${seconds}s` : `${minutes}m ${seconds}s`;
}

/**
 * Compute-time window of a job: started_at to completed_at. A job that is still
 * pending/running has no end (measured to now). A stopped job without
 * completed_at (paused, or recorded before migration 020) ends at updated_at.
 * A job without started_at (recorded before migration 020, or a v1 job from
 * before v1 recorded it) starts at created_at, which includes its queueing.
 */
export function jobRunWindow(job: {
  status: string;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  updated_at: string;
}): { start: string | null; end: string | null } {
  const running = job.status === 'PENDING' || job.status === 'IN_PROGRESS';
  return {
    start: job.started_at ?? job.created_at,
    end: job.completed_at ?? (running ? null : job.updated_at),
  };
}

// What the backend list endpoints accept: page >= 1, 1 <= page_size <= 100
export const MAX_PAGE_SIZE = 100;
export const DEFAULT_PAGE_SIZE = 20;

export function clampPage(page: number): number {
  const p = Math.floor(page);
  return Number.isFinite(p) && p >= 1 ? p : 1;
}

export function clampPageSize(size: number): number {
  const s = Math.floor(size);
  return Number.isFinite(s) && s >= 1 ? Math.min(s, MAX_PAGE_SIZE) : DEFAULT_PAGE_SIZE;
}

// Page/size URL params, clamped so a hand-edited URL can't 422
export function parsePageParams(page?: string | null, size?: string | null): { page: number; size: number } {
  return { page: clampPage(Number(page)), size: clampPageSize(Number(size)) };
}

/** A job id from a URL: a whole number in 1..2^31-1 (the database's int), else null */
export function parseJobId(raw: string): number | null {
  if (!/^\d+$/.test(raw)) return null;
  const id = Number(raw);
  return id >= 1 && id <= 2147483647 ? id : null;
}

// Pagination utilities
export function getPageNumbers(currentPage: number, totalPages: number): (number | string)[] {
  if (totalPages <= 0) return [];

  if (currentPage < 1) currentPage = 1;
  else if (currentPage > totalPages) currentPage = totalPages;

  const pages: (number | string)[] = [];
  const delta = 2;

  if (totalPages <= 7) {
    for (let i = 1; i <= totalPages; i++) {
      pages.push(i);
    }
    return pages;
  }

  pages.push(1);

  const rangeStart = Math.max(2, currentPage - delta);
  const rangeEnd = Math.min(totalPages - 1, currentPage + delta);

  if (rangeStart > 2) pages.push('...');

  for (let i = rangeStart; i <= rangeEnd; i++) {
    pages.push(i);
  }

  if (rangeEnd < totalPages - 1) pages.push('...');

  pages.push(totalPages);

  return pages;
}
// Thousands-separated count with a fixed locale, so server-rendered markup
// matches the client regardless of the browser locale (avoids hydration errors)
const countFormatter = new Intl.NumberFormat('en-US');
export function formatCount(n: number): string {
  return countFormatter.format(n);
}
