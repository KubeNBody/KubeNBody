// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import {
  PREDICTOR_RUNGS,
  composePredictorMethod,
  effectiveOrder,
  ALL_INTEGRATION_METHODS,
  integrationMethodLabel,
  parsePredictorMethod,
  partitionStrategyLabel,
  supportsPredictor,
  type PredictorRung,
} from '@/lib/format-integration';

describe('predictor methods', () => {
  it('compose and parse are inverse for every supported pair', () => {
    for (const [base, rungs] of Object.entries(PREDICTOR_RUNGS)) {
      for (const rung of rungs) {
        expect(parsePredictorMethod(composePredictorMethod(base, rung))).toEqual({ base, rung });
      }
    }
  });

  it('composes the backend names', () => {
    expect(composePredictorMethod('rk4', 'accel')).toBe('grk4_accel');
    expect(composePredictorMethod('velocity_verlet', 'frozen')).toBe('gvv_frozen');
    expect(composePredictorMethod('forest_ruth', 'linear')).toBe('gfr_linear');
  });

  it('passes Euler methods through and falls back to linear for unknown rungs', () => {
    expect(composePredictorMethod('simple_euler', 'accel')).toBe('simple_euler');
    expect(composePredictorMethod('rk4', 'bogus' as PredictorRung)).toBe('grk4_linear');
    expect(supportsPredictor('symplectic_euler')).toBe(false);
  });

  it('returns null for base and unknown names', () => {
    expect(parsePredictorMethod('rk4')).toBeNull();
    expect(parsePredictorMethod('grk4_cubic')).toBeNull();
  });

  it('caps the global order at the nominal order', () => {
    expect(effectiveOrder('velocity_verlet', 'accel')).toBe(2);
    expect(effectiveOrder('rk4', 'accel')).toBe(3);
    expect(effectiveOrder('rk4', 'frozen')).toBe(1);
  });
});

describe('labels', () => {
  it('labels g-variants with their base integrator and rung', () => {
    expect(integrationMethodLabel('grk4_accel')).toBe('RK4 · accel');
    expect(integrationMethodLabel('gvv_frozen')).toBe('Velocity Verlet · frozen');
  });

  it('defaults a missing method to Simple Euler and keeps unknown names', () => {
    expect(integrationMethodLabel(null)).toBe('Simple Euler');
    expect(integrationMethodLabel('leapfrog')).toBe('leapfrog');
  });

  it('lists the 5 bases and the 9 predictor variants', () => {
    expect(ALL_INTEGRATION_METHODS).toHaveLength(14);
    expect(ALL_INTEGRATION_METHODS).toContain('gfr_accel');
  });

  it('labels partition strategies, defaulting to linear', () => {
    expect(partitionStrategyLabel('orb')).toBe('ORB');
    expect(partitionStrategyLabel(undefined)).toBe('Linear');
  });
});
