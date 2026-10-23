// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Build-time access to the recorded demo fixtures (demo-data/, or DEMO_DATA_DIR),
// for the server components that the static demo build prerenders. Only reached
// from demo-build modules (lib/data/demo/server.ts, the *.demo.tsx routes).
import { promises as fs } from 'fs';
import path from 'path';
import { fixturePaths, type DemoManifest } from '@/lib/demo/query';

const DEMO_DATA_DIR = process.env.DEMO_DATA_DIR || path.join(process.cwd(), 'demo-data');

export async function readFixture<T>(relativePath: string): Promise<T> {
  return JSON.parse(await fs.readFile(path.join(DEMO_DATA_DIR, relativePath), 'utf8'));
}

export function readManifest(): Promise<DemoManifest> {
  return readFixture<DemoManifest>(fixturePaths.manifest);
}
