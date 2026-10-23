// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import {
  formatCount,
  formatDuration,
  formatFileSize,
  getPageNumbers,
  jobRunWindow,
  parseApiDate,
  parseJobId,
  parsePageParams,
} from '@/lib/utils';

describe('formatters', () => {
  it('formats file sizes', () => {
    expect(formatFileSize(0)).toBe('0 B');
    expect(formatFileSize(205)).toBe('205.00 B');
    expect(formatFileSize(1536)).toBe('1.50 KB');
    expect(formatFileSize(5 * 1024 ** 4)).toBe('5120.00 GB');
  });

  it('formats durations', () => {
    expect(formatDuration(-5)).toBe('0m 0s');
    expect(formatDuration(39_000)).toBe('0m 39s');
    expect(formatDuration(3_723_000)).toBe('1h 2m 3s');
  });

  it('formats counts with a fixed locale', () => {
    expect(formatCount(1234567)).toBe('1,234,567');
  });
});

describe('parseApiDate', () => {
  it('reads naive timestamps as UTC', () => {
    expect(parseApiDate('2026-10-09T13:21:51')?.toISOString()).toBe('2026-10-09T13:21:51.000Z');
    expect(parseApiDate('2026-10-09T13:21:51+02:00')?.toISOString()).toBe('2026-10-09T11:21:51.000Z');
  });

  it('returns null for empty or invalid input', () => {
    expect(parseApiDate(null)).toBeNull();
    expect(parseApiDate('not a date')).toBeNull();
  });
});

describe('jobRunWindow', () => {
  const base = { created_at: 'c', updated_at: 'u' };
  it('measures a running job to now', () => {
    expect(jobRunWindow({ ...base, status: 'IN_PROGRESS', started_at: 's' })).toEqual({ start: 's', end: null });
  });
  it('ends a stopped job without completed_at at updated_at', () => {
    expect(jobRunWindow({ ...base, status: 'PAUSED' })).toEqual({ start: 'c', end: 'u' });
  });
});

describe('pagination', () => {
  it('clamps page params to what the backend accepts', () => {
    expect(parsePageParams('0', '500')).toEqual({ page: 1, size: 100 });
    expect(parsePageParams('x', undefined)).toEqual({ page: 1, size: 20 });
    expect(parsePageParams('3', '50')).toEqual({ page: 3, size: 50 });
    expect(parsePageParams(null, null)).toEqual({ page: 1, size: 20 });
  });

  it('accepts only whole job ids the database can hold', () => {
    expect(parseJobId('42')).toBe(42);
    for (const bad of ['0', '-1', '12.5', '12abc', '', ' 7', '2147483648']) expect(parseJobId(bad)).toBeNull();
  });

  it('lists page numbers with ellipses', () => {
    expect(getPageNumbers(1, 0)).toEqual([]);
    expect(getPageNumbers(2, 5)).toEqual([1, 2, 3, 4, 5]);
    expect(getPageNumbers(10, 20)).toEqual([1, '...', 8, 9, 10, 11, 12, '...', 20]);
    expect(getPageNumbers(99, 20)).toEqual([1, '...', 18, 19, 20]);
  });
});
