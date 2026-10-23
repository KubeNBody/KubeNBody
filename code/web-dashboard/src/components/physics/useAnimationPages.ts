// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AnimationSnapshot } from '@/types/api';
import { apiClient, isAbortError } from '@/lib/api-client';

// Snapshots per page are capped so that one page holds at most this many
// particle rows (the JSON response grows with N x snapshots). The backend's
// paginated animation endpoint caps `limit` with the same budget
// (animation_cache.effective_page_limit): keep the two equal.
const PAGE_PARTICLE_BUDGET = 2e5;

// A failed page is retried automatically this many times, with exponential
// backoff. After that only the Retry button loads it again
const MAX_AUTO_ATTEMPTS = 3;
const RETRY_BASE_DELAY_MS = 1000;

// Frames the playhead advances per animation tick at 1x speed (AnimationViewer's
// play loop), for runs of up to RATE_REFERENCE_SNAPSHOTS snapshots: 3 per second,
// about 100 s for such a run. Runs with more snapshots play proportionally
// faster, so 1x still shows the whole run in about that time (the extra
// snapshots make the motion smoother, not slower). The pages playback is about
// to need are derived from the rate
const FRAMES_PER_TICK = 0.05;
const RATE_REFERENCE_SNAPSHOTS = 300;

function framesPerTick(totalSnapshots: number, playbackSpeed: number): number {
  return FRAMES_PER_TICK * playbackSpeed * Math.max(1, totalSnapshots / RATE_REFERENCE_SNAPSHOTS);
}

interface PageFailure {
  attempts: number;
  message: string;
  // Earliest time of the next automatic attempt (ms since epoch)
  retryAt: number;
}

/**
 * Paged snapshot loading for the animation viewer: loads the page under the
 * playhead, prefetches ahead of playback, evicts pages far behind it, and
 * tracks failed pages (backoff, retry count) so a failing page is neither
 * re-requested on every frame nor its error cleared by the next prefetch.
 */
export function useAnimationPages({
  jobId,
  snapshotsPerPage,
  currentFrame,
  isPlaying,
  playbackSpeed,
}: {
  jobId: number;
  snapshotsPerPage: number;
  currentFrame: number;
  isPlaying: boolean;
  playbackSpeed: number;
}) {
  const [loadedPages, setLoadedPages] = useState<Map<number, AnimationSnapshot[]>>(new Map());
  const [loadingPages, setLoadingPages] = useState<Set<number>>(new Set());
  const [failedPages, setFailedPages] = useState<Map<number, PageFailure>>(new Map());
  const [totalSnapshots, setTotalSnapshots] = useState(0);
  // Snapshots per page: snapshotsPerPage until N is known from page 0, then
  // shrunk for large N (PAGE_PARTICLE_BUDGET)
  const [pageSize, setPageSize] = useState(snapshotsPerPage);
  const pageSizeRef = useRef(snapshotsPerPage);
  const [particleCount, setParticleCount] = useState(0);
  // The run's first snapshot, kept after page 0 is evicted: sizes and colours
  // are derived from it once (masses do not change during a run)
  const [firstSnapshot, setFirstSnapshot] = useState<AnimationSnapshot | undefined>(undefined);
  const seekAbortControllerRef = useRef<AbortController | null>(null);  // Cancel previous seeks

  // Refs for stable access to loading state (avoid re-creating loadPage on every state change)
  const loadedPagesRef = useRef(loadedPages);
  const loadingPagesRef = useRef(loadingPages);
  const failedPagesRef = useRef(failedPages);
  loadedPagesRef.current = loadedPages;
  loadingPagesRef.current = loadingPages;
  failedPagesRef.current = failedPages;

  const totalPages = Math.ceil(totalSnapshots / pageSize);
  const currentPage = Math.floor(currentFrame / pageSize);
  const maxFrame = totalSnapshots - 1;

  // A seek passes its AbortSignal so a later seek can cancel the requests it no
  // longer needs. `force` (the Retry button) loads a page past its failures.
  // `backoffElapsed` (the retry timer) skips the retryAt check, which a timer
  // firing a little early would otherwise fail with nothing left to reschedule it
  const loadPage = useCallback(async (
    pageIndex: number,
    { signal, force = false, backoffElapsed = false }: { signal?: AbortSignal; force?: boolean; backoffElapsed?: boolean } = {}
  ) => {
    if (loadedPagesRef.current.has(pageIndex) || loadingPagesRef.current.has(pageIndex)) return;

    const failure = failedPagesRef.current.get(pageIndex);
    if (failure && !force) {
      if (failure.attempts >= MAX_AUTO_ATTEMPTS) return;
      if (!backoffElapsed && Date.now() < failure.retryAt) return;
    }

    // Mark it in the ref too, so another call before the next render dedupes
    loadingPagesRef.current = new Set(loadingPagesRef.current).add(pageIndex);
    setLoadingPages(prev => new Set(prev).add(pageIndex));

    try {
      const offset = pageIndex * pageSize;
      const data = await apiClient.getJobAnimationDataPaginated(jobId, offset, pageSize, signal);

      // Requested with a page size that has since changed: indices no longer match
      if (pageSize !== pageSizeRef.current) return;

      const n = data.snapshots[0]?.particles.length ?? 0;
      const sizeForN = n > 0
        ? Math.max(1, Math.min(snapshotsPerPage, Math.floor(PAGE_PARTICLE_BUDGET / n)))
        : pageSize;

      if (pageIndex === 0) {
        setTotalSnapshots(data.total_snapshots);
        setParticleCount(n);
        setFirstSnapshot(data.snapshots[0]);
      }

      if (pageIndex === 0 && sizeForN < pageSize) {
        // N is known now: switch to smaller pages, keeping the snapshots already
        // fetched as the first pages (a partial chunk only if it is the last page)
        const pages = new Map<number, AnimationSnapshot[]>();
        for (let start = 0; start < data.snapshots.length; start += sizeForN) {
          const chunk = data.snapshots.slice(start, start + sizeForN);
          if (chunk.length === sizeForN || start + chunk.length >= data.total_snapshots) {
            pages.set(start / sizeForN, chunk);
          }
        }
        pageSizeRef.current = sizeForN;
        setPageSize(sizeForN);
        setLoadedPages(pages);
        // Failures were recorded under the old page indices
        setFailedPages(new Map());
      } else {
        setLoadedPages(prev => new Map(prev).set(pageIndex, data.snapshots));
        setFailedPages(prev => {
          if (!prev.has(pageIndex)) return prev;
          const updated = new Map(prev);
          updated.delete(pageIndex);
          return updated;
        });
      }
    } catch (error) {
      // Cancelled by a newer seek: not an error (the page reloads if still needed)
      if (isAbortError(error)) return;
      console.error(`Failed to load page ${pageIndex}:`, error);
      const message = error instanceof Error ? error.message : String(error);
      setFailedPages(prev => {
        // A manual retry starts a new round of automatic attempts
        const attempts = force ? 1 : (prev.get(pageIndex)?.attempts ?? 0) + 1;
        const retryAt = Date.now() + RETRY_BASE_DELAY_MS * 2 ** (attempts - 1);
        return new Map(prev).set(pageIndex, { attempts, message, retryAt });
      });
    } finally {
      setLoadingPages(prev => {
        const updated = new Set(prev);
        updated.delete(pageIndex);
        return updated;
      });
    }
  }, [jobId, pageSize, snapshotsPerPage]);

  // Pages the playhead needs now: those of the snapshots it draws (its own and
  // the next one it interpolates towards) and, while playing, those of the frame
  // the next tick moves to, which playback holds on (isFrameReady). A page that
  // blocks playback is thus retried and reported like the current one
  const rate = framesPerTick(totalSnapshots, playbackSpeed);
  const playTarget = isPlaying ? currentFrame + rate : currentFrame;
  const lastNeededIndex = Math.min(Math.floor(playTarget) + 1, maxFrame);
  const lastNeededPage = Math.max(currentPage, Math.floor(lastNeededIndex / pageSize));
  const neededPages = useMemo(
    () => Array.from({ length: lastNeededPage - currentPage + 1 }, (_, i) => currentPage + i),
    [currentPage, lastNeededPage]
  );

  // Load the current page and prefetch with speed-aware lookahead. Depends on
  // the playhead's position bands within the page, not on the frame itself
  // (which changes every tick)
  const frameInPage = currentFrame - currentPage * pageSize;
  const pastHalf = frameInPage > pageSize * 0.5;
  const nearEnd = frameInPage > pageSize * 0.9;
  const nearStart = frameInPage < pageSize * 0.3;
  // By the rate relative to 1x of a reference run: 1x = 2 pages, 2x = 2, 4x = 3,
  // up to 8 for snapshot-dense runs (whose pages go by faster)
  const relativeRate = rate / FRAMES_PER_TICK;
  const pagesToPrefetch = Math.min(Math.ceil(Math.max(relativeRate, 1) / 2) + 1, 8);

  useEffect(() => {
    loadPage(currentPage);

    // Prefetch ahead from the middle of a page while playing (smoother playback),
    // and near its end in any case (no stutter on stepping/resuming)
    if ((isPlaying && pastHalf) || nearEnd) {
      for (let i = 1; i <= pagesToPrefetch; i++) {
        if (currentPage + i < totalPages) {
          loadPage(currentPage + i);
        }
      }
    }

    // Playback only moves forward. The previous page is for stepping back
    if (currentPage > 0 && nearStart) {
      loadPage(currentPage - 1);
    }
  }, [currentPage, pastHalf, nearEnd, nearStart, isPlaying, pagesToPrefetch, totalPages, loadPage]);

  // A needed page that is not loaded: a seek may have cancelled it (it was still
  // marked as loading when the newer seek asked for it), or it failed. Load it
  // once it is free again, a failed one after its backoff
  useEffect(() => {
    const timers: ReturnType<typeof setTimeout>[] = [];
    for (const page of neededPages) {
      if (loadedPages.has(page) || loadingPages.has(page)) continue;
      const failure = failedPages.get(page);
      if (!failure) {
        loadPage(page);
      } else if (failure.attempts < MAX_AUTO_ATTEMPTS) {
        timers.push(setTimeout(
          () => loadPage(page, { backoffElapsed: true }),
          Math.max(0, failure.retryAt - Date.now())
        ));
      }
    }
    return () => timers.forEach(clearTimeout);
  }, [neededPages, loadedPages, loadingPages, failedPages, loadPage]);

  // Abort in-flight seek requests on unmount
  useEffect(() => () => seekAbortControllerRef.current?.abort(), []);

  // Cache eviction - only evict if we have more than 5 pages loaded (increased for better prefetching)
  useEffect(() => {
    setLoadedPages(prev => {
      // Skip eviction if we have 5 or fewer pages (allows for aggressive prefetching)
      if (prev.size <= 5) return prev;

      const pagesToKeep = new Set([currentPage - 1, currentPage, currentPage + 1, currentPage + 2, currentPage + 3].filter(p => p >= 0 && p < totalPages));
      let hasChanges = false;

      // Check if any pages need eviction
      for (const pageIndex of prev.keys()) {
        if (!pagesToKeep.has(pageIndex)) {
          hasChanges = true;
          break;
        }
      }

      // Only create new Map if there are changes
      if (!hasChanges) return prev;

      const updated = new Map();
      for (const [pageIndex, data] of prev.entries()) {
        if (pagesToKeep.has(pageIndex)) updated.set(pageIndex, data);
      }
      return updated;
    });
  }, [currentPage, totalPages]);

  // Burst prefetch on a seek: the target page and its neighbours, cancelling
  // the previous seek's requests (rapid slider dragging would flood the API)
  const seekTo = useCallback((frame: number) => {
    const newPage = Math.floor(frame / pageSize);
    if (Math.abs(newPage - currentPage) <= 1) return;

    seekAbortControllerRef.current?.abort();
    const { signal } = (seekAbortControllerRef.current = new AbortController());

    // loadPage dedupes, so these never duplicate a request
    loadPage(newPage, { signal }); // Target page
    if (newPage > 0) loadPage(newPage - 1, { signal }); // Previous
    if (newPage < totalPages - 1) loadPage(newPage + 1, { signal }); // Next
    if (newPage < totalPages - 2) loadPage(newPage + 2, { signal }); // Next+1
  }, [pageSize, currentPage, totalPages, loadPage]);

  // Whether the snapshots to draw at `frame` are loaded (its snapshot, and the
  // next one it interpolates towards). Playback holds the playhead otherwise.
  const isFrameReady = useCallback((frame: number) => {
    const index = Math.floor(frame);
    if (!loadedPagesRef.current.has(Math.floor(index / pageSize))) return false;
    return index + 1 > maxFrame || loadedPagesRef.current.has(Math.floor((index + 1) / pageSize));
  }, [pageSize, maxFrame]);

  // The current page's snapshots, plus the next page's first one when loaded,
  // so the last stretch of a page interpolates instead of stalling
  const pageSnapshots = loadedPages.get(currentPage);
  const nextFirstSnapshot = loadedPages.get(currentPage + 1)?.[0];
  const snapshots = useMemo(() => {
    if (!pageSnapshots) return [];
    return nextFirstSnapshot && pageSnapshots.length === pageSize
      ? [...pageSnapshots, nextFirstSnapshot]
      : pageSnapshots;
  }, [pageSnapshots, nextFirstSnapshot, pageSize]);

  // The first failure among the pages the playhead waits for
  const pageError = neededPages.map(page => failedPages.get(page)).find(Boolean) ?? null;

  const retry = useCallback(() => {
    for (const page of neededPages) loadPage(page, { force: true });
  }, [neededPages, loadPage]);

  return {
    framesPerTick: rate,
    pageSize,
    totalSnapshots,
    particleCount,
    firstSnapshot,
    maxFrame,
    currentPage,
    snapshots,
    loadedPageCount: loadedPages.size,
    isLoading: loadingPages.size > 0,
    // The playhead waits for a page (shown as a loading overlay)
    isWaiting: neededPages.some(page => !loadedPages.has(page)) && loadingPages.size > 0,
    pageError: pageError && {
      message: pageError.message,
      // Another automatic attempt is still due
      retrying: pageError.attempts < MAX_AUTO_ATTEMPTS,
    },
    retry,
    seekTo,
    isFrameReady,
  };
}
