// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import { formatLogTick, formatValue, powerOfTen } from '@/lib/chart-format';

describe('chart number formats', () => {
  it('keeps readable numbers plain', () => {
    expect(formatValue(1)).toBe('1');
    expect(formatValue(0.25)).toBe('0.25');
    expect(formatValue(-1.2834)).toBe('-1.28');
    expect(formatValue(1200)).toBe('1200');
    expect(formatValue(0)).toBe('0');
  });

  it('uses short exponents outside that range', () => {
    expect(formatValue(8.492e-6)).toBe('8.49e-6');
    expect(formatValue(1.4e7)).toBe('1.4e7');
    expect(formatValue(NaN)).toBe('—');
  });

  it('typesets powers of ten for log axes', () => {
    expect(powerOfTen(-6)).toBe('10⁻⁶');
    expect(powerOfTen(12)).toBe('10¹²');
    expect(formatLogTick(1e-6)).toBe('10⁻⁶');
    expect(formatLogTick(3e-6)).toBe('');
  });
});
