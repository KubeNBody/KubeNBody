// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { getFiles } from '@/lib/data/server';
import { FilesList } from '@/components/files/FilesList';
import { parsePageParams } from '@/lib/utils';

// Sort keys the backend accepts (routes/files.py sort_by pattern)
const SORT_KEYS: readonly string[] = ['name', 'created_at', 'last_used_at', 'size'];

// Force dynamic rendering - don't pre-render at build time
export const dynamic = 'force-dynamic';

interface PageProps {
  searchParams: Promise<{
    page?: string;
    size?: string;
    search?: string;
    sort_by?: string;
  }>;
}

export default async function FilesPage({ searchParams }: PageProps) {
  // Await searchParams (Next.js 15 requirement)
  const params = await searchParams;

  // Parse search params
  const { page, size } = parsePageParams(params.page, params.size);
  const search = params.search || '';
  const sort_by = params.sort_by && SORT_KEYS.includes(params.sort_by) ? params.sort_by : 'created_at';

  // Server-side data fetching - runs on Next.js server, can access k8s services
  const data = await getFiles({
    page,
    size,
    search: search || undefined,
    sort_by,
  });

  return (
    <FilesList files={data.files} total={data.total} page={data.page} size={data.size} pages={data.pages} />
  );
}
