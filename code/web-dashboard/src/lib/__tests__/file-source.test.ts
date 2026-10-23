// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import { emptyUpload, fileSourceReady, fileSourceReducer, type FileSource } from '@/components/submit/file-source';

const file = (name: string) => new File(['n,idBody,m,x,y,z,vx,vy,vz\n'], name);

describe('fileSourceReducer', () => {
  it('applies a validation only to the file it was for', () => {
    const a = file('a.csv');
    const b = file('b.csv');
    let s: FileSource = fileSourceReducer(emptyUpload, { type: 'picked', file: a });
    s = fileSourceReducer(s, { type: 'picked', file: b });
    s = fileSourceReducer(s, { type: 'validated', file: a, validation: { valid: true } });
    expect(s).toMatchObject({ file: b, validating: true, validation: null });
    s = fileSourceReducer(s, { type: 'validated', file: b, validation: { valid: true } });
    expect(fileSourceReady(s)).toBe(true);
  });

  it('switching modes drops the other mode entirely', () => {
    let s = fileSourceReducer(emptyUpload, { type: 'picked', file: file('a.csv') });
    s = fileSourceReducer(s, { type: 'mode', kind: 'existing' });
    expect(s).toEqual({ kind: 'existing', fileId: null });
    expect(fileSourceReady(s)).toBe(false);
    expect(fileSourceReady(fileSourceReducer(s, { type: 'existing', fileId: 5 }))).toBe(true);
  });

  it('a rejected drop clears the previous file and keeps the reason', () => {
    let s = fileSourceReducer(emptyUpload, { type: 'picked', file: file('a.csv') });
    s = fileSourceReducer(s, { type: 'rejected', error: 'too large' });
    expect(s).toMatchObject({ kind: 'upload', file: null, validation: { valid: false, error: 'too large' } });
  });
});
