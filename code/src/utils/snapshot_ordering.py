# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Canonical body ordering for cross-job snapshot analysis.

Comparison/analysis matches bodies ACROSS jobs positionally (by row index):
``TrajectoryPairing`` interpolates snapshots element-wise and
``compute_trajectory_rmse`` differences ``pos_a[i] - pos_b[i]``. That is only
valid if every job presents its bodies in one canonical order.

Monolithic jobs and V2 jobs with the 'linear' partition keep the input-file
order, but the 'random', 'spatial' and 'orb' partitions permute the bodies once
at job creation, so two jobs can disagree on row order, which would silently
misalign a per-body trajectory RMSE between, say, a 'spatial' and a 'linear'
job.

``idBody`` is the stable per-particle identity: it is preserved across epochs and
is independent of the partition strategy and the engine (V1/V2). Reordering both
sides into ascending ``idBody`` before comparison aligns identical bodies
regardless of how each job was ordered. Kept as a standalone, dependency-light
module so it is unit-testable without the DB/S3 stack.
"""

from typing import Optional, Tuple

import numpy as np


SnapshotData = Tuple[np.ndarray, np.ndarray, np.ndarray]  # (positions, velocities, masses)


def canonicalize_by_id(
    positions: np.ndarray,
    velocities: np.ndarray,
    masses: np.ndarray,
    ids: Optional[np.ndarray],
) -> SnapshotData:
    """Return (positions, velocities, masses) reordered into ascending idBody.

    For a job already in idBody order — monolithic, V2 'linear', and any IC whose
    idBody column is sequential — this is a no-op fast-path (an O(N) scan, no
    copy), so existing comparisons are byte-for-byte unchanged. It only reorders
    the permuted ('random'/'spatial') jobs, making mixed-order comparisons align
    identical bodies.

    ``ids=None`` (identity unavailable, e.g. a legacy CSV with no idBody column)
    leaves the order untouched — no worse than the historical behaviour.
    """
    if ids is None:
        return positions, velocities, masses

    ids = np.asarray(ids)
    # Already ascending -> avoid the argsort + fancy-index copy entirely. Covers
    # the overwhelmingly common case (file-order jobs) so the streaming metrics
    # path pays only one O(N) comparison per snapshot.
    if ids.size == 0 or bool(np.all(ids[:-1] <= ids[1:])):
        return positions, velocities, masses

    order = np.argsort(ids, kind="stable")
    return positions[order], velocities[order], masses[order]
