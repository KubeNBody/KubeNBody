// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Number formatting for chart axes and tooltips (pure, tested).

const SUPERSCRIPT: Record<string, string> = { '-': '⁻', '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴', '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹' };

/** 1e-6 -> "10⁻⁶" (a power of ten as typeset on a log axis) */
export function powerOfTen(exponent: number): string {
  return `10${String(exponent).replace(/[-\d]/g, (c) => SUPERSCRIPT[c])}`;
}

/**
 * A tick or value label: plain decimals in a readable range (0.25, 1200), short
 * exponent form outside it (3.2e-6, 1.4e+7), never "1.00e+0".
 */
export function formatValue(v: number, digits = 3): string {
  if (!Number.isFinite(v)) return '—';
  if (v === 0) return '0';
  const a = Math.abs(v);
  if (a >= 1e-3 && a < 1e5) return Number(v.toPrecision(digits)).toString();
  const [mantissa, exp] = v.toExponential(digits - 1).split('e');
  return `${Number(mantissa)}e${Number(exp)}`;
}

/** Log-axis tick: powers of ten as 10ⁿ, the in-between ticks unlabelled */
export function formatLogTick(v: number): string {
  const exp = Math.log10(v);
  return Math.abs(exp - Math.round(exp)) < 1e-9 ? powerOfTen(Math.round(exp)) : '';
}
