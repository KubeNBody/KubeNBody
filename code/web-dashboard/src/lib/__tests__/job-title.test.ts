// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import { jobTitle } from '@/lib/domain/job-title';

describe('jobTitle', () => {
  it('prefers the name, then the file, then the number', () => {
    expect(jobTitle({ job_id: 7, name: 'Galaxies on four workers', filename: 'galaxy.csv' })).toBe('Galaxies on four workers');
    expect(jobTitle({ job_id: 7, name: '  ', filename: 'galaxy.csv' })).toBe('galaxy.csv');
    expect(jobTitle({ job_id: 7, name: null, filename: null })).toBe('Job 7');
  });
});
