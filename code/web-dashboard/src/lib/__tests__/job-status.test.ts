// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import {
  canCancelStatus,
  canDeleteStatus,
  canForceRecoverStatus,
  canPauseStatus,
  canRecoverStatus,
  canViewAnimationFor,
  canResumeStatus,
  jobStatusInfo,
  recoverConfirmPrompt,
  snapshotCount,
} from '@/lib/domain/job-status';

// src/enums.py JobStatus
const STATUSES = ['PENDING', 'IN_PROGRESS', 'PAUSED', 'COMPLETED', 'FAILED', 'CANCELLED'];

describe('jobStatusInfo', () => {
  it('covers every backend status with its own label', () => {
    const labels = STATUSES.map((s) => jobStatusInfo(s).label);
    expect(new Set(labels).size).toBe(STATUSES.length);
    expect(labels).not.toContain('PENDING');
  });

  it('gives cancelled and paused different icons and tones', () => {
    expect(jobStatusInfo('CANCELLED').icon).not.toBe(jobStatusInfo('PAUSED').icon);
    expect(jobStatusInfo('CANCELLED').tone).not.toBe(jobStatusInfo('PAUSED').tone);
  });

  it('shows an unknown status as itself, in the neutral tone', () => {
    expect(jobStatusInfo('ARCHIVED')).toMatchObject({ label: 'ARCHIVED', tone: 'neutral' });
  });
});

describe('lifecycle gates', () => {
  it('match the transitions the backend accepts', () => {
    const allowed = (f: (s: string) => boolean) => STATUSES.filter(f);
    expect(allowed(canCancelStatus)).toEqual(['PENDING', 'IN_PROGRESS', 'PAUSED']);
    expect(allowed(canPauseStatus)).toEqual(['PENDING', 'IN_PROGRESS']);
    expect(allowed(canResumeStatus)).toEqual(['PAUSED']);
    expect(allowed(canDeleteStatus)).toEqual(['COMPLETED', 'FAILED', 'CANCELLED']);
  });

  it('offers recovery only for v2 jobs', () => {
    for (const s of STATUSES) {
      expect(canRecoverStatus('monolithic', s)).toBe(false);
      expect(canForceRecoverStatus('distributed', s)).toBe(false);
    }
    expect(STATUSES.filter((s) => canRecoverStatus('distributed_v2', s))).toEqual([
      'IN_PROGRESS',
      'PAUSED',
      'FAILED',
    ]);
    expect(STATUSES.filter((s) => canForceRecoverStatus('distributed_v2', s))).toEqual(['COMPLETED']);
  });

  it('confirms every recovery that throws work away', () => {
    expect(recoverConfirmPrompt('FAILED')).toBeNull();
    expect(recoverConfirmPrompt('CANCELLED')).toBeNull();
    for (const s of ['IN_PROGRESS', 'PAUSED', 'COMPLETED']) {
      expect(recoverConfirmPrompt(s)).toMatch(/snapshot/);
    }
  });
});

describe('animation availability', () => {
  const job = (job_type: string, status: string, current_epoch: number, num_epochs: number, snapshot_interval: number) =>
    ({ job_type, status, current_epoch, num_epochs, snapshot_interval });

  it('counts snapshots like the backend', () => {
    expect(snapshotCount(0, 10, true)).toBe(0);
    expect(snapshotCount(25, 10, false)).toBe(2);
    expect(snapshotCount(25, 10, true)).toBe(3);
    expect(snapshotCount(20, 10, true)).toBe(2);
    expect(snapshotCount(25, 0, true)).toBe(1);
    expect(snapshotCount(25, 0, false)).toBe(0);
  });

  it('offers a running monolithic or V1 job once it has a snapshot', () => {
    expect(canViewAnimationFor(job('monolithic', 'IN_PROGRESS', 4, 100, 5))).toBe(false);
    expect(canViewAnimationFor(job('monolithic', 'IN_PROGRESS', 5, 100, 5))).toBe(true);
    expect(canViewAnimationFor(job('distributed', 'PAUSED', 50, 100, 0))).toBe(false);
    expect(canViewAnimationFor(job('monolithic', 'COMPLETED', 100, 100, 0))).toBe(true);
  });

  it('needs two snapshots for a V2 job', () => {
    expect(canViewAnimationFor(job('distributed_v2', 'COMPLETED', 500, 500, 500))).toBe(false);
    expect(canViewAnimationFor(job('distributed_v2', 'COMPLETED', 501, 501, 500))).toBe(true);
    expect(canViewAnimationFor(job('distributed_v2', 'COMPLETED', 100, 100, 0))).toBe(false);
    expect(canViewAnimationFor(job('distributed_v2', 'IN_PROGRESS', 15, 100, 10))).toBe(false);
    expect(canViewAnimationFor(job('distributed_v2', 'IN_PROGRESS', 20, 100, 10))).toBe(true);
  });
});
