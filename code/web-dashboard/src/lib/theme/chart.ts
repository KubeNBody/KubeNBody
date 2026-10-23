// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Chart colours: the design tokens (styles/globals.css) as literal values for
// SVG attributes, plus the categorical series palette.

export const CHART = {
  surface: '#131c2e', // --surface
  axisText: '#a3b1c6', // --fg-muted
  axisLine: '#3a4a68', // --border-strong
  grid: '#24314a', // --border
  crosshair: '#8b9ab1', // --fg-subtle
  reference: '#e8eef7', // --fg: the reference run
} as const;

/**
 * Comparison series, in fixed order (never cycled by rank). Validated with the
 * dataviz palette checker on --surface (#131c2e), adjacent pairs: lightness
 * band, chroma floor, CVD ΔE >= 8.4 (protan), normal-vision ΔE >= 19.3,
 * contrast >= 3:1. Status red/green are left out on purpose.
 */
export const SERIES_COLORS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#9085e9'] as const;

/** Past six series the hues repeat with a dash pattern (composite encoding), not as new colours */
export function seriesStyle(index: number): { color: string; dash?: string } {
  const color = SERIES_COLORS[index % SERIES_COLORS.length];
  return index < SERIES_COLORS.length ? { color } : { color, dash: '6 3' };
}
