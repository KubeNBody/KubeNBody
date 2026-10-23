// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import { DIMENSION_BADGE, forceAlgorithmLabel, jobTypeLabel } from '@/lib/domain/badges';
import { comparisonStatusInfo } from '@/lib/domain/comparison-status';
import { driftQuality } from '@/lib/domain/drift';
import { jobStatusInfo } from '@/lib/domain/job-status';
import { TONE_BADGE } from '@/lib/domain/tones';

describe('driftQuality', () => {
  it('grades relative energy drift (percent) with one set of thresholds', () => {
    expect(driftQuality(0.005).level).toBe('excellent');
    expect(driftQuality(0.05).level).toBe('good');
    expect(driftQuality(0.5)).toMatchObject({ level: 'fair', tone: 'warning' });
    expect(driftQuality(8.0162)).toMatchObject({ level: 'poor', tone: 'danger' });
  });

  it('reads the magnitude, whatever the sign', () => {
    expect(driftQuality(-0.5).level).toBe('fair');
  });

  it('marks NaN/Infinity as diverged', () => {
    expect(driftQuality(NaN)).toMatchObject({ label: 'Diverged', tone: 'danger' });
  });
});

describe('status tones', () => {
  it('puts a waiting job and a waiting comparison on the same tone', () => {
    expect(comparisonStatusInfo('PENDING').tone).toBe(jobStatusInfo('PENDING').tone);
    expect(comparisonStatusInfo('COMPUTING').tone).toBe(jobStatusInfo('IN_PROGRESS').tone);
    expect(comparisonStatusInfo('FAILED').tone).toBe(jobStatusInfo('FAILED').tone);
  });

  it('never reuses a status colour for an identity badge', () => {
    // The status tokens (globals.css) are Tailwind's green/blue/yellow/orange/red
    // 400 and a slate grey. Identity badges must use other hue families
    const statusFamilies = ['green', 'emerald', 'blue', 'sky', 'yellow', 'amber', 'orange', 'red', 'slate', 'gray'];
    for (const cls of Object.values(DIMENSION_BADGE)) {
      const family = /bg-([a-z]+)-\d/.exec(cls)?.[1];
      expect(family).toBeDefined();
      expect(statusFamilies).not.toContain(family);
    }
    for (const cls of Object.values(TONE_BADGE)) expect(cls).toMatch(/^bg-(success|info|warning|caution|danger|neutral)\//);
  });
});

describe('labels', () => {
  it('names job types and force algorithms', () => {
    expect(jobTypeLabel('distributed_v2')).toBe('Distributed V2');
    expect(jobTypeLabel('unknown_type')).toBe('unknown_type');
    expect(forceAlgorithmLabel(null)).toBe('Direct');
    expect(forceAlgorithmLabel('barnes_hut', true)).toBe('Barnes-Hut O(N log N)');
  });
});
