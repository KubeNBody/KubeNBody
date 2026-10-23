#!/usr/bin/env node
// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Serves the exported static demo (out/) the way GitHub Pages does:
// under the base path, directory URLs -> index.html, unknown paths -> 404.html.
// Serving out/ from the root instead would hide basePath mistakes.
//
// Usage: NEXT_PUBLIC_BASE_PATH=/KubeNBody-demo node scripts/preview-static-demo.mjs [port]
import { createReadStream, existsSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const outDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'out');
const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? '';
const port = Number(process.argv[2]) || 8502;

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.csv': 'text/csv; charset=utf-8',
  '.gz': 'application/gzip',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
};

if (!existsSync(outDir)) {
  console.error('No export found - run `npm run build:demo` first');
  process.exit(1);
}

function send(res, status, file) {
  res.writeHead(status, { 'Content-Type': TYPES[extname(file)] ?? 'application/octet-stream' });
  createReadStream(file).pipe(res);
}

createServer((req, res) => {
  const url = new URL(req.url ?? '/', 'http://localhost');
  if (basePath && url.pathname === '/') {
    res.writeHead(302, { Location: `${basePath}/` }).end();
    return;
  }
  if (!url.pathname.startsWith(`${basePath}/`)) {
    res.writeHead(404).end('Outside the base path');
    return;
  }
  const relativePath = normalize(decodeURIComponent(url.pathname.slice(basePath.length))).replace(/^(\.\.[/\\])+/, '');
  let file = join(outDir, relativePath);
  if (existsSync(file) && statSync(file).isDirectory()) {
    // Pages redirects /dir to /dir/ before serving its index.html
    if (!url.pathname.endsWith('/')) {
      res.writeHead(301, { Location: `${url.pathname}/${url.search}` }).end();
      return;
    }
    file = join(file, 'index.html');
  }
  if (existsSync(file)) {
    send(res, 200, file);
  } else {
    send(res, 404, join(outDir, '404.html'));
  }
}).listen(port, () => {
  console.log(`Static demo: http://localhost:${port}${basePath}/`);
});
