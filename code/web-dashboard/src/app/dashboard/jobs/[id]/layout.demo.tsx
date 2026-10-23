// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Demo build only. Static export needs every [id] known at build time.
// a parent layout's generateStaticParams covers both jobs/[id] and
// jobs/[id]/animation, so neither page has to change.
import { readManifest } from '@/lib/data/demo/fixtures-fs';

export const dynamicParams = false;

export async function generateStaticParams() {
  const manifest = await readManifest();
  return manifest.job_ids.map((id) => ({ id: String(id) }));
}

export default function DemoJobLayout({ children }: { children: React.ReactNode }) {
  return children;
}
