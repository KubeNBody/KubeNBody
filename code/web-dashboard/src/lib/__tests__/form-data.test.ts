// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import { toFormData } from '@/lib/form-data';

describe('toFormData', () => {
  it('skips missing fields and stringifies the rest', () => {
    const f = toFormData({ num_epochs: 1000, time_step: 1e-10, use_gpu: false, adaptive_eta: undefined, file_id: null, job_type: 'monolithic' });
    expect([...f.keys()]).toEqual(['num_epochs', 'time_step', 'use_gpu', 'job_type']);
    expect(f.get('time_step')).toBe('1e-10');
    expect(f.get('use_gpu')).toBe('false');
  });

  it('keeps files as files', () => {
    const file = new File(['n,idBody'], 'bodies.csv');
    expect(toFormData({ file }).get('file')).toBeInstanceOf(File);
  });
});
