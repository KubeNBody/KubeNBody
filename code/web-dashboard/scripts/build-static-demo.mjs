#!/usr/bin/env node
// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Builds the read-only static demo of the dashboard.
//
// The demo builds in place from the same src/ as the normal build: with
// NEXT_PUBLIC_DEMO_MODE=1, next.config.js picks the *.demo.tsx routes instead of the
// *.server.ts(x) ones (middleware, proxy, server actions are what a static export
// rejects) and swaps the data layer for its fixture-backed implementation
// (src/lib/data/demo/). `next build` exports to out/. The recorded fixtures
// (demo-data/) are read from disk while prerendering and copied into out/ for the
// browser.
//
// Usage: NEXT_PUBLIC_BASE_PATH=/KubeNBody-demo node scripts/build-static-demo.mjs
//        (empty/unset base path = site served from the domain root,
//         DEMO_DATA_DIR=<dir> builds from other fixtures than demo-data/)
import { execFileSync } from 'node:child_process';
import { cpSync, existsSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const appDir = join(dirname(fileURLToPath(import.meta.url)), '..');
const outDir = join(appDir, 'out');
const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? '';
// Fixtures recorded by scripts/export_demo_fixtures.py (override to try other data)
const dataDir = resolve(process.env.DEMO_DATA_DIR ?? join(appDir, 'demo-data'));

function fail(message) {
  console.error(`\n✗ ${message}`);
  process.exit(1);
}

if (basePath && (!basePath.startsWith('/') || basePath.endsWith('/'))) {
  fail(`NEXT_PUBLIC_BASE_PATH must look like "/repo" (got "${basePath}")`);
}
if (!existsSync(join(dataDir, 'manifest.json'))) {
  fail(`${dataDir}/manifest.json is missing - record fixtures first (scripts/export_demo_fixtures.py)`);
}

const manifest = JSON.parse(readFileSync(join(dataDir, 'manifest.json'), 'utf8'));
// Static export treats an empty generateStaticParams() as missing and aborts
if (!manifest.job_ids?.length || !manifest.comparison_ids?.length) {
  fail('the fixtures need at least one job and one saved comparison (see manifest.json)');
}

rmSync(outDir, { recursive: true, force: true });

// A static export still builds in .next: keep a normal (server) build that is
// there aside, and put it back afterwards, so building the demo doesn't break it
const serverBuild = join(appDir, '.next');
const keptServerBuild = join(appDir, '.next-server-kept');
rmSync(keptServerBuild, { recursive: true, force: true });
const keepServer = existsSync(join(serverBuild, 'standalone'));
if (keepServer) renameSync(serverBuild, keptServerBuild);
const restoreServerBuild = () => {
  if (!keepServer) return;
  rmSync(serverBuild, { recursive: true, force: true });
  renameSync(keptServerBuild, serverBuild);
};
process.on('exit', restoreServerBuild);
console.log(`→ next build (static export, base path "${basePath || '/'}")`);
execFileSync(join(appDir, 'node_modules', '.bin', 'next'), ['build'], {
  cwd: appDir,
  stdio: 'inherit',
  env: {
    ...process.env,
    NEXT_PUBLIC_DEMO_MODE: '1',
    NEXT_PUBLIC_BASE_PATH: basePath,
    NEXT_TELEMETRY_DISABLED: '1',
    DEMO_DATA_DIR: dataDir,
  },
});

// Fetched by the browser at runtime (lib/demo.ts DEMO_DATA_URL)
cpSync(dataDir, join(outDir, 'demo-data'), { recursive: true });

// GitHub Pages' branch deploys run Jekyll, which drops directories starting with "_" (_next/)
writeFileSync(join(outDir, '.nojekyll'), '');

console.log('→ Checking the export');
const expectedPages = [
  'index.html',
  '404.html',
  'login/index.html',
  'dashboard/index.html',
  'dashboard/jobs/index.html',
  'dashboard/files/index.html',
  'dashboard/submit/index.html',
  'dashboard/compare/index.html',
  'dashboard/comparisons/index.html',
  ...manifest.job_ids.flatMap((id) => [`dashboard/jobs/${id}/index.html`, `dashboard/jobs/${id}/animation/index.html`]),
  ...manifest.comparison_ids.map((id) => `dashboard/comparisons/${id}/index.html`),
  'demo-data/manifest.json',
];
const missing = expectedPages.filter((page) => !existsSync(join(outDir, page)));
if (missing.length) fail(`Export is missing pages:\n  ${missing.join('\n  ')}`);

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

const files = walk(outDir);
const totalBytes = files.reduce((sum, file) => sum + statSync(file).size, 0);
const totalMb = totalBytes / 1024 / 1024;
if (totalMb > 50) fail(`Export is ${totalMb.toFixed(1)} MB (> 50 MB budget)`);

console.log(`✓ Static demo exported to ${relative(process.cwd(), outDir) || outDir} (${files.length} files, ${totalMb.toFixed(1)} MB)`);
