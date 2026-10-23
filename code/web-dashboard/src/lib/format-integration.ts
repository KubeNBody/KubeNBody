// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Integration-method presentation + the distributed "predictor family" mapping.
//
// The backend exposes a flat enum. Alongside the 5 base integrators it has a
// "g-" family for the distributed (package) flow — gvv_/grk4_/gfr_* — where the
// name encodes a base integrator PLUS a background-predictor rung that controls
// how the other packages' bodies are extrapolated to each sub-stage time:
//
//   frozen  -> held at epoch start        -> global order 1
//   linear  -> x + tau*v                  -> global order 2
//   accel   -> x + tau*v + 1/2 tau^2 a    -> global order 3   (rk4 / forest_ruth)
//
// Velocity-Verlet saturates at order 2, but its `accel` variant (gvv_accel)
// reproduces the monolithic step exactly and needs one force evaluation per
// task instead of two. This module is the single source of truth
// the submit form uses to COMPOSE a g-name and the job views use to PARSE one.

import { IntegrationMethod } from '@/types/api';

export type PredictorRung = 'frozen' | 'linear' | 'accel';

// Which rungs each multi-stage base integrator supports, in display order.
export const PREDICTOR_RUNGS: Record<string, PredictorRung[]> = {
  velocity_verlet: ['frozen', 'linear', 'accel'],
  rk4: ['frozen', 'linear', 'accel'],
  forest_ruth: ['frozen', 'linear', 'accel'],
};

// Human copy + resulting global convergence order for each rung.
export const PREDICTOR_META: Record<
  PredictorRung,
  { label: string; short: string; order: number; formula: string }
> = {
  frozen: { label: 'Frozen', short: 'frozen', order: 1, formula: 'background held at epoch start' },
  linear: { label: 'Linear', short: 'linear', order: 2, formula: 'x + τ·v' },
  accel: { label: 'Acceleration', short: 'accel', order: 3, formula: 'x + τ·v + ½·τ²·a' },
};

// Nominal (monolithic) order of each multi-stage base. The distributed global
// order is min(rung order, nominal), so VV saturates at 2 even with `accel`.
const NOMINAL_ORDER: Record<string, number> = { velocity_verlet: 2, rk4: 4, forest_ruth: 4 };

/** Guaranteed distributed global order for a (base, rung) pair. */
export function effectiveOrder(base: string, rung: PredictorRung): number {
  const nominal = NOMINAL_ORDER[base] ?? PREDICTOR_META[rung].order;
  return Math.min(PREDICTOR_META[rung].order, nominal);
}

const G_PREFIX: Record<string, string> = {
  velocity_verlet: 'gvv',
  rk4: 'grk4',
  forest_ruth: 'gfr',
};

/** True if this base integrator has a distributed predictor family. */
export function supportsPredictor(base: string): boolean {
  return base in PREDICTOR_RUNGS;
}

/**
 * (base, rung) -> flat backend method name, e.g. ('rk4','accel') => 'grk4_accel'.
 * Euler / unknown bases have no predictor variant and pass through unchanged.
 * Defensive: a rung the base doesn't support falls back to 'linear'.
 */
export function composePredictorMethod(base: string, rung: PredictorRung): IntegrationMethod {
  const prefix = G_PREFIX[base];
  if (!prefix) return base as IntegrationMethod;
  const allowed = PREDICTOR_RUNGS[base];
  const safe = allowed.includes(rung) ? rung : 'linear';
  return `${prefix}_${safe}` as IntegrationMethod;
}

/** Inverse of composePredictorMethod. Base names (and unknowns) return null. */
export function parsePredictorMethod(method: string): { base: string; rung: PredictorRung } | null {
  const m = /^g(vv|rk4|fr)_(frozen|linear|accel)$/.exec(method);
  if (!m) return null;
  const base = m[1] === 'vv' ? 'velocity_verlet' : m[1] === 'rk4' ? 'rk4' : 'forest_ruth';
  return { base, rung: m[2] as PredictorRung };
}

const BASE_LABEL: Record<string, string> = {
  simple_euler: 'Simple Euler',
  symplectic_euler: 'Symplectic Euler',
  velocity_verlet: 'Velocity Verlet',
  rk4: 'RK4',
  forest_ruth: 'Forest-Ruth',
};

/** Base integrators in display order */
export const BASE_INTEGRATORS = Object.keys(BASE_LABEL);

/** Every stored method name: the bases, then each base's predictor family */
export const ALL_INTEGRATION_METHODS: string[] = [
  ...BASE_INTEGRATORS,
  ...Object.entries(PREDICTOR_RUNGS).flatMap(([base, rungs]) => rungs.map((r) => composePredictorMethod(base, r))),
];

/**
 * Display name of any stored method - a plain base name or a g-variant
 * ("RK4 · accel"). The badge colour is the integrator dimension's
 * (lib/domain/badges.ts), the same for every method.
 */
export function integrationMethodLabel(method: string | null | undefined): string {
  if (!method) return 'Simple Euler';
  const parsed = parsePredictorMethod(method);
  if (parsed) return `${BASE_LABEL[parsed.base] ?? parsed.base} · ${PREDICTOR_META[parsed.rung].short}`;
  return BASE_LABEL[method] ?? method;
}

/**
 * Display name of the V2 package partition strategy. 'linear' is the
 * historical default (file order). 'random'/'spatial'/'orb' change which bodies
 * share a package (for predicted multi-stage methods, and so the error).
 */
export function partitionStrategyLabel(strategy: string | null | undefined): string {
  switch (strategy) {
    case 'random':
      return 'Random';
    case 'spatial':
      return 'Spatial (Z-order)';
    case 'orb':
      return 'ORB';
    default:
      return 'Linear';
  }
}
