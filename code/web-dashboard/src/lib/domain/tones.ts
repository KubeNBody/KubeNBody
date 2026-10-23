// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Status colours. A tone means the same thing on every page: success = done
// or good, info = running, warning = paused / needs a look, caution = waiting,
// danger = failed or bad, neutral = stopped. Identity badges (job type,
// algorithm, integrator) use the separate dimension hues in ./badges.ts, never
// these, so a colour is never both a status and a category.
// (Full class strings: Tailwind only generates classes it finds verbatim. The
// colours are the status tokens in styles/globals.css.)
export type Tone = 'success' | 'info' | 'warning' | 'caution' | 'danger' | 'neutral';

/** Tinted pill: background, text and border */
export const TONE_BADGE: Record<Tone, string> = {
  success: 'bg-success/10 text-success border-success/30',
  info: 'bg-info/10 text-info border-info/30',
  warning: 'bg-warning/10 text-warning border-warning/30',
  caution: 'bg-caution/10 text-caution border-caution/30',
  danger: 'bg-danger/10 text-danger border-danger/30',
  neutral: 'bg-neutral/10 text-neutral border-neutral/30',
};

/** Icon or text in the tone */
export const TONE_TEXT: Record<Tone, string> = {
  success: 'text-success',
  info: 'text-info',
  warning: 'text-warning',
  caution: 'text-caution',
  danger: 'text-danger',
  neutral: 'text-neutral',
};

/** Icon tile background */
export const TONE_TILE: Record<Tone, string> = {
  success: 'bg-success/10',
  info: 'bg-info/10',
  warning: 'bg-warning/10',
  caution: 'bg-caution/10',
  danger: 'bg-danger/10',
  neutral: 'bg-neutral/10',
};

/** Solid fill (progress bars) */
export const TONE_FILL: Record<Tone, string> = {
  success: 'bg-success',
  info: 'bg-info',
  warning: 'bg-warning',
  caution: 'bg-caution',
  danger: 'bg-danger',
  neutral: 'bg-neutral',
};

/** For SVG/canvas: the same values as the --success ... --neutral tokens in globals.css */
export const TONE_HEX: Record<Tone, string> = {
  success: '#4ade80',
  info: '#60a5fa',
  warning: '#facc15',
  caution: '#fb923c',
  danger: '#f87171',
  neutral: '#a3b1c6',
};
