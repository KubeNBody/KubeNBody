// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { AlertTriangle, CheckCircle, Clock, Loader2, XCircle, type LucideIcon } from 'lucide-react';
import { TONE_BADGE, type Tone } from './tones';

// Saved-comparison statuses (src/comparison_worker.py), on the same
// tones as job statuses: waiting is orange and running is blue on every page.
export interface ComparisonStatusInfo {
  icon: LucideIcon;
  spin?: boolean;
  tone: Tone;
  label: string;
  badge: string;
}

const info = (icon: LucideIcon, tone: Tone, label: string, spin = false): ComparisonStatusInfo => ({
  icon,
  tone,
  label,
  spin,
  badge: TONE_BADGE[tone],
});

const CONFIG: Record<string, ComparisonStatusInfo> = {
  PENDING: info(Clock, 'caution', 'Pending'),
  COMPUTING: info(Loader2, 'info', 'Computing', true),
  COMPLETED: info(CheckCircle, 'success', 'Completed'),
  COMPLETED_EMPTY: info(AlertTriangle, 'warning', 'No overlap'),
  FAILED: info(XCircle, 'danger', 'Failed'),
};

export function comparisonStatusInfo(status: string): ComparisonStatusInfo {
  return CONFIG[status] ?? info(AlertTriangle, 'neutral', status);
}
