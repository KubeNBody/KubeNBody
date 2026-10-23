// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import type { Tone } from './tones';

// One reading of relative energy drift |ΔE/E₀| (in percent) for the summary
// table, the compare badges and the drift chart, which used three different
// sets of thresholds before.
export type DriftLevel = 'excellent' | 'good' | 'fair' | 'poor';

export interface DriftQuality {
  level: DriftLevel;
  label: string;
  tone: Tone;
}

export const DRIFT_THRESHOLDS_PERCENT = { excellent: 0.01, good: 0.1, fair: 1 } as const;

export function driftQuality(driftPercent: number): DriftQuality {
  const d = Math.abs(driftPercent);
  if (!Number.isFinite(d)) return { level: 'poor', label: 'Diverged', tone: 'danger' };
  if (d < DRIFT_THRESHOLDS_PERCENT.excellent) return { level: 'excellent', label: 'Excellent', tone: 'success' };
  if (d < DRIFT_THRESHOLDS_PERCENT.good) return { level: 'good', label: 'Good', tone: 'success' };
  if (d < DRIFT_THRESHOLDS_PERCENT.fair) return { level: 'fair', label: 'Fair', tone: 'warning' };
  return { level: 'poor', label: 'Poor', tone: 'danger' };
}
