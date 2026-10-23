// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Read-only demo build (static export).
// next.config.js always defines NEXT_PUBLIC_DEMO_MODE, so this is a build-time
// constant (false in the normal build). The demo's UI branches in other modules
// stay in the bundle but are unreachable. The fixture adapter is a lazy chunk
// that the normal build never loads.
export const IS_DEMO = process.env.NEXT_PUBLIC_DEMO_MODE === '1';

// Static hosting under a sub-path (GitHub Pages project site). Next.js prefixes
// <Link>/router URLs itself. This is for hand-built URLs (fixture fetches, downloads).
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? '';

export const DEMO_USERNAME = process.env.NEXT_PUBLIC_DEMO_USERNAME || 'demo';
export const DEMO_PASSWORD = process.env.NEXT_PUBLIC_DEMO_PASSWORD || 'demo';

export const DEMO_DATA_URL = `${BASE_PATH}/demo-data`;
export const DEMO_READ_ONLY_MESSAGE = 'This is a read-only demo';

const DEMO_AUTH_KEY = 'nbody-demo-auth';

export function isDemoAuthenticated(): boolean {
  try {
    return window.localStorage.getItem(DEMO_AUTH_KEY) === '1';
  } catch {
    return false;
  }
}

export function setDemoAuthenticated(value: boolean): void {
  try {
    if (value) {
      window.localStorage.setItem(DEMO_AUTH_KEY, '1');
    } else {
      window.localStorage.removeItem(DEMO_AUTH_KEY);
    }
  } catch {
    // Storage blocked (private mode etc.) - the gate is cosmetic anyway
  }
}

export function resultsDownloadUrl(jobId: number, jobType: string): string {
  if (IS_DEMO) {
    return `${DEMO_DATA_URL}/jobs/${jobId}/results.csv.gz`;
  }
  return jobType === 'distributed_v2' ? `/api/v2/jobs/${jobId}/results` : `/api/jobs/${jobId}/results`;
}

export function fileDownloadUrl(fileId: number): string {
  if (IS_DEMO) {
    return `${DEMO_DATA_URL}/files/${fileId}.csv`; // fixturePaths.fileContent
  }
  return `/api/files/${fileId}/download`;
}
