// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Demo build only. Static export needs every saved comparison id at
// build time. The (client) page itself stays unchanged.
import { readManifest } from '@/lib/data/demo/fixtures-fs';

export const dynamicParams = false;

export async function generateStaticParams() {
  const manifest = await readManifest();
  return manifest.comparison_ids.map((id) => ({ id: String(id) }));
}

export default function DemoComparisonLayout({ children }: { children: React.ReactNode }) {
  return children;
}
