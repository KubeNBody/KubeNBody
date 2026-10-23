#!/usr/bin/env node
// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Contrast check of the design tokens in src/styles/globals.css:
// every text token against every surface, and white text on every solid fill,
// must reach WCAG AA for body text (4.5:1). Also checks that TONE_HEX in
// lib/domain/tones.ts matches the status tokens. Exit 1 on any failure.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const appDir = join(dirname(fileURLToPath(import.meta.url)), '..');
const css = readFileSync(join(appDir, 'src/styles/globals.css'), 'utf8');
const root = css.slice(css.indexOf(':root {'), css.indexOf('}', css.indexOf(':root {')));
const tokens = Object.fromEntries([...root.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)].map((m) => [m[1], m[2]]));

const channel = (c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const luminance = (hex) => {
  const [r, g, b] = [1, 3, 5].map((i) => channel(parseInt(hex.slice(i, i + 2), 16) / 255));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a, b) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

const SURFACES = ['bg', 'surface-sunken', 'surface', 'surface-raised'];
const TEXTS = ['fg', 'fg-muted', 'fg-subtle', 'link', 'ring', 'success', 'info', 'warning', 'caution', 'danger', 'neutral'];
const FILLS = ['primary', 'primary-hover', 'success-solid', 'danger-solid'];
const AA = 4.5;

let failures = 0;
const check = (fg, bg, label) => {
  const r = ratio(fg, bg);
  if (r < AA) failures++;
  return `${r < AA ? '✗' : ' '} ${label.padEnd(34)} ${r.toFixed(2)}`;
};
for (const name of [...SURFACES, ...TEXTS, ...FILLS]) {
  if (!tokens[name]) {
    console.error(`✗ token --${name} missing from :root`);
    failures++;
  }
}
for (const t of TEXTS) for (const s of SURFACES) console.log(check(tokens[t], tokens[s], `--${t} on --${s}`));
for (const f of FILLS) console.log(check('#ffffff', tokens[f], `white on --${f}`));

const tones = readFileSync(join(appDir, 'src/lib/domain/tones.ts'), 'utf8');
const hexBlock = tones.slice(tones.indexOf('TONE_HEX'));
for (const tone of ['success', 'info', 'warning', 'caution', 'danger', 'neutral']) {
  const m = new RegExp(`${tone}: '(#[0-9a-fA-F]{6})'`).exec(hexBlock);
  if (!m || m[1].toLowerCase() !== tokens[tone]?.toLowerCase()) {
    console.error(`✗ TONE_HEX.${tone} (${m?.[1]}) differs from --${tone} (${tokens[tone]})`);
    failures++;
  }
}

console.log(failures ? `\n✗ ${failures} failure(s)` : '\n✓ all token pairs reach 4.5:1');
process.exit(failures ? 1 : 0);
