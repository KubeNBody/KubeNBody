// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Identity badges: one hue per dimension (job type, force algorithm,
// integrator, partitioning, GPU), none of them a status hue (./tones.ts), so
// the label says which value and the colour says which kind of thing it is.
export type Dimension = 'jobType' | 'algorithm' | 'integrator' | 'partition' | 'gpu';

export const DIMENSION_BADGE: Record<Dimension, string> = {
  jobType: 'bg-violet-400/10 text-violet-300 border-violet-400/25',
  algorithm: 'bg-cyan-400/10 text-cyan-300 border-cyan-400/25',
  integrator: 'bg-teal-400/10 text-teal-300 border-teal-400/25',
  partition: 'bg-fuchsia-400/10 text-fuchsia-300 border-fuchsia-400/25',
  gpu: 'bg-pink-400/10 text-pink-300 border-pink-400/25',
};

/** Job types the backend accepts (src/enums.py JobType), in display order */
export const JOB_TYPES = [
  { value: 'monolithic', label: 'Monolithic' },
  { value: 'distributed', label: 'Distributed' },
  { value: 'distributed_v2', label: 'Distributed V2' },
] as const;

export type JobTypeValue = (typeof JOB_TYPES)[number]['value'];

export function jobTypeLabel(value: string | null | undefined): string {
  return JOB_TYPES.find((t) => t.value === value)?.label ?? value ?? 'Unknown';
}

export const FORCE_ALGORITHMS = [
  { value: 'direct', label: 'Direct', long: 'Direct O(N²)' },
  { value: 'barnes_hut', label: 'Barnes-Hut', long: 'Barnes-Hut O(N log N)' },
] as const;

export function forceAlgorithmLabel(value: string | null | undefined, long = false): string {
  const a = FORCE_ALGORITHMS.find((x) => x.value === (value || 'direct'));
  return a ? (long ? a.long : a.label) : (value ?? 'Direct');
}
