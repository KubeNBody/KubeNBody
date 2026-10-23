// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useEffect, useRef } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';

/**
 * Filters, page and page size of a server-rendered list kept in the URL's
 * search params (the page re-fetches when they change). Shared by the jobs and
 * files lists.
 */
export function useUrlFilters(
  basePath: string,
  { initialPage, initialSize, total, pages }: { initialPage: number; initialSize: number; total: number; pages: number }
) {
  const router = useRouter();
  const searchParams = useSearchParams();

  // Validate page parameter (must be positive integer)
  const pageParam = Number(searchParams.get('page'));
  const currentPage = (pageParam > 0 && Number.isInteger(pageParam)) ? pageParam : initialPage;

  // Validate size parameter (must be positive integer)
  const sizeParam = Number(searchParams.get('size'));
  const currentSize = (sizeParam > 0 && sizeParam <= 100 && Number.isInteger(sizeParam)) ? sizeParam : initialSize;

  // Latest params, so a debounced search does not undo a filter clicked meanwhile
  const searchParamsRef = useRef(searchParams);
  searchParamsRef.current = searchParams;
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
  }, []);

  const buildFilterUrl = (key: string, value: string) => {
    const params = new URLSearchParams(searchParamsRef.current);

    if (value === '') {
      params.delete(key);
    } else {
      params.set(key, value);
    }

    // Reset page when changing filters (except for page itself)
    if (key !== 'page') {
      params.delete('page');
    }

    return `${basePath}?${params.toString()}`;
  };

  const handleFilterChange = (key: string, value: string) => {
    router.push(buildFilterUrl(key, value));
  };

  // Several keys in one navigation (calling handleFilterChange per key would build
  // each URL from the same not-yet-updated params, so only the last one would stick)
  const clearFilters = (keys: readonly string[]) => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    const params = new URLSearchParams(searchParamsRef.current);
    for (const key of keys) params.delete(key);
    params.delete('page');
    router.push(`${basePath}?${params.toString()}`);
  };

  // Search: debounced, and replace instead of push so each keystroke is not a history entry
  const handleSearchChange = (value: string) => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    searchTimerRef.current = setTimeout(() => {
      searchTimerRef.current = null;
      router.replace(buildFilterUrl('search', value));
    }, 300);
  };

  // Past the last page (deleting its last item, or a stale link) the list is
  // empty although there are items: step back to the last page
  const pastLastPage = total > 0 && currentPage > pages;
  useEffect(() => {
    if (pastLastPage) router.replace(buildFilterUrl('page', String(pages)));
    // buildFilterUrl reads the latest params through a ref
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pastLastPage, pages, router]);

  return { searchParams, currentPage, currentSize, pastLastPage, handleFilterChange, handleSearchChange, clearFilters };
}
