// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// ε² is typically tiny (1e-9) or O(1). Use scientific for extremes, fixed otherwise.
export function formatSofteningSq(v: number | null | undefined): string {
  if (v == null) return '—';
  if (v === 0) return '0';
  const abs = Math.abs(v);
  if (abs < 0.01 || abs >= 1000) return v.toExponential(2);
  return parseFloat(v.toPrecision(4)).toString();
}
