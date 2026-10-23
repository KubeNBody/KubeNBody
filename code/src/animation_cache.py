# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""In-memory cache for animation data with TTL."""
import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple


# Cache structure: (job_id, offset, limit) -> (data, timestamp), oldest first
_animation_cache: "OrderedDict[Tuple[int, int, int], Tuple[List[dict], float]]" = OrderedDict()
_cache_lock = Lock()

# Full animation data cache: job_id -> (animation_data, timestamp, estimated
# bytes), least recently used first
_full_animation_cache: "OrderedDict[int, Tuple[Dict[str, Any], float, int]]" = OrderedDict()
_full_cache_lock = Lock()

CACHE_TTL = 300  # 5 minutes
FULL_CACHE_TTL = 600  # 10 minutes for full animation data
# Bound on all cached full animations together (one larger is not cached).
# An animation file is capped at 50 MB of JSON (file_service), ~5x that once
# parsed, so the largest still fit.
MAX_FULL_CACHE_MB = 384
# Python size of one parsed particle [x, y, z, m]: a 4-item list (88 B), four
# floats (4 x 24 B) and its slot in the snapshot's list (8 B), as measured
# after json.loads
BYTES_PER_PARTICLE = 192
# Pages kept (least recently used dropped first): a page of the viewer's
# 2e5-particle budget is tens of MB as Python lists
MAX_CACHED_PAGES = 8
# Particles (snapshots x N) one animation page may carry: the page limit is
# clamped to it, so a large-N job's page stays tens of MB. The same budget as
# the dashboard's AnimationViewer PAGE_PARTICLE_BUDGET, which then never asks
# for more (and re-slices a clamped first page into its own page size).
PAGE_PARTICLE_BUDGET = 200_000


def effective_page_limit(limit: int, n_particles: int) -> int:
    """The number of snapshots a page of `limit` returns for N particles."""
    if n_particles <= 0:
        return limit
    return max(1, min(limit, PAGE_PARTICLE_BUDGET // n_particles))


def _estimated_size(data: Dict[str, Any]) -> int:
    return sum(len(s.get("particles", ())) for s in data.get("snapshots", ())) * BYTES_PER_PARTICLE


def get_cached_full_animation(job_id: int) -> Optional[Dict[str, Any]]:
    """Get cached full animation data if it exists and hasn't expired."""
    with _full_cache_lock:
        if job_id in _full_animation_cache:
            data, timestamp, _ = _full_animation_cache[job_id]
            if time.time() - timestamp < FULL_CACHE_TTL:
                _full_animation_cache.move_to_end(job_id)
                return data
            del _full_animation_cache[job_id]
    return None


def cache_full_animation(job_id: int, data: Dict[str, Any]) -> bool:
    """Cache full animation data, evicting the least recently used ones to
    stay within MAX_FULL_CACHE_MB.

    Returns True if cached, False if too large to cache.
    """
    try:
        size = _estimated_size(data)
    except Exception:
        return False        # not an animation we can size: do not hold it
    limit = MAX_FULL_CACHE_MB * 1024 * 1024
    if size > limit:
        return False

    with _full_cache_lock:
        _full_animation_cache.pop(job_id, None)
        total = sum(entry[2] for entry in _full_animation_cache.values())
        while _full_animation_cache and total + size > limit:
            _, (_, _, evicted) = _full_animation_cache.popitem(last=False)
            total -= evicted
        _full_animation_cache[job_id] = (data, time.time(), size)
    return True


def get_cached_snapshots(job_id: int, offset: int, limit: int) -> Optional[List[dict]]:
    """Get cached snapshots if they exist and haven't expired."""
    key = (job_id, offset, limit)

    with _cache_lock:
        if key in _animation_cache:
            data, timestamp = _animation_cache[key]
            if time.time() - timestamp < CACHE_TTL:
                _animation_cache.move_to_end(key)
                return data
            del _animation_cache[key]

    return None


def cache_snapshots(job_id: int, offset: int, limit: int, data: List[dict]):
    """Cache snapshots with current timestamp (of completed jobs only: a
    running job's pages and snapshot count change)."""
    key = (job_id, offset, limit)

    with _cache_lock:
        _animation_cache[key] = (data, time.time())
        _animation_cache.move_to_end(key)
        while len(_animation_cache) > MAX_CACHED_PAGES:
            _animation_cache.popitem(last=False)


def clear_expired_cache():
    """Remove all expired entries (periodic cleanup)."""
    current_time = time.time()
    deleted_count = 0

    # Clear expired page cache
    with _cache_lock:
        keys_to_delete = [
            k for k, (_, timestamp) in _animation_cache.items()
            if current_time - timestamp >= CACHE_TTL
        ]
        for key in keys_to_delete:
            del _animation_cache[key]
        deleted_count += len(keys_to_delete)

    # Clear expired full animation cache
    with _full_cache_lock:
        jobs_to_delete = [
            job_id for job_id, (_, timestamp, _) in _full_animation_cache.items()
            if current_time - timestamp >= FULL_CACHE_TTL
        ]
        for job_id in jobs_to_delete:
            del _full_animation_cache[job_id]
        deleted_count += len(jobs_to_delete)

    return deleted_count
