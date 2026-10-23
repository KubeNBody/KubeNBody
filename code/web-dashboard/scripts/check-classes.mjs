#!/usr/bin/env node
// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Colours come from the design tokens (styles/globals.css), not
// from raw Tailwind palette classes. Raw palette classes are allowed only where
// the palette is the point: components/ui (shadcn primitives), lib/domain (the
// identity-badge hues) and the black scrims over the 3D canvas. Also keeps the
// demo flag out of components. Run by `npm run lint`.
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const appDir = join(dirname(fileURLToPath(import.meta.url)), '..');
const srcDir = join(appDir, 'src');
const ALLOWED_DIRS = ['components/ui/', 'lib/domain/', 'lib/__tests__/'];
const ALLOWED_CLASSES = { 'components/physics/': /^bg-black\/\d+$/ };

const RAW = /(?<![\w/-])(?:[a-z0-9-]+:)*((?:text|bg|border|ring|from|to|via|divide|placeholder|fill|stroke|outline|decoration|accent|shadow)-(?:white|black|slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)(?:-\d+)?(?:\/\d+)?)(?![\w/-])/g;

const walk = (dir) =>
  readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : /\.(tsx?|jsx?)$/.test(name) ? [p] : [];
  });

let found = 0;
for (const file of walk(srcDir)) {
  const rel = relative(srcDir, file);
  if (ALLOWED_DIRS.some((d) => rel.startsWith(d))) continue;
  const allowed = Object.entries(ALLOWED_CLASSES).find(([d]) => rel.startsWith(d))?.[1];
  readFileSync(file, 'utf8')
    .split('\n')
    .forEach((line, i) => {
      for (const m of line.matchAll(RAW)) {
        if (allowed?.test(m[1])) continue;
        found++;
        console.error(`src/${rel}:${i + 1}: raw palette class "${m[0]}" - use a token (text-fg, bg-surface, text-danger, ...)`);
      }
    });
}
// The demo flag stays in the modules that define what the build can do. Components
// ask lib/capabilities (canMutate, liveData, ...) instead
const DEMO_FLAG_ALLOWED = ['lib/demo.ts', 'lib/demo/', 'lib/capabilities.ts', 'lib/api-client.ts', 'components/demo/', 'lib/data/demo/'];
let flagged = 0;
for (const file of walk(srcDir)) {
  const rel = relative(srcDir, file);
  if (DEMO_FLAG_ALLOWED.some((d) => rel.startsWith(d))) continue;
  readFileSync(file, 'utf8')
    .split('\n')
    .forEach((line, i) => {
      if (/\bIS_DEMO\b/.test(line)) {
        flagged++;
        console.error(`src/${rel}:${i + 1}: IS_DEMO outside the demo modules - use lib/capabilities`);
      }
    });
}

if (found || flagged) {
  if (found) console.error(`\n✗ ${found} raw palette class(es), see styles/globals.css for the tokens`);
  if (flagged) console.error(`✗ ${flagged} IS_DEMO check(s) outside lib/demo* and lib/capabilities`);
  process.exit(1);
}
console.log('✓ no raw palette classes outside components/ui and lib/domain, IS_DEMO only in the demo/capability modules');
