# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The source positions of a package force evaluation in the distributed
package steppers (physics.py), shared with the Barnes-Hut tree kernels."""

import numpy as np
from numba import jit


@jit(nopython=True, nogil=True, cache=True)
def _global_indices_for_ids(k_ids: np.ndarray, all_ids: np.ndarray) -> np.ndarray:
    """(K,) int64 index of each package body ID in all_ids (-1 where absent).

    Sort + binary search: O(N log N + K log N) total. Shared by the direct and
    Barnes-Hut package steppers to locate each package body's row in the global
    arrays (so its current stage position can be scattered into the source set)."""
    order = np.argsort(all_ids)
    sorted_ids = all_ids[order]
    n = len(sorted_ids)
    k = len(k_ids)
    indices = np.empty(k, dtype=np.int64)
    for i in range(k):
        pos = np.searchsorted(sorted_ids, k_ids[i])
        if pos < n and sorted_ids[pos] == k_ids[i]:
            indices[i] = order[pos]
        else:
            indices[i] = -1
    return indices


@jit(nopython=True, nogil=True, cache=True)
def _stage_source_positions(
    all_positions: np.ndarray,
    all_velocities: np.ndarray,
    all_accelerations: np.ndarray,
    tau: float,
    predictor_order: int,
    k_stage_positions: np.ndarray,
    global_indices: np.ndarray,
) -> np.ndarray:
    """
    Source positions for a package force evaluation at stage-time offset tau.

    Distributed package steppers compute forces on K bodies using all N bodies
    as sources, but only the K package bodies are actually integrated during the
    step. The other N-K "background" bodies are EXTRAPOLATED to the stage time by
    a Taylor predictor whose order is selected by `predictor_order`:

      0 (frozen): x0                       -> O(dt) source error, global order 1
                  (kept as a control)
      1 (linear): x0 + tau*v               -> O(dt^2) source error, global order 2
      2 (accel):  x0 + tau*v + 0.5*tau^2*a -> O(dt^3) source error, global order 3

    On top of the predicted background, the package's own current stage positions
    are scattered over their global rows (exact intra-package coupling). Global
    order = predictor_order + 1: a predictor carrying terms through tau^p leaves a
    position error O(tau^(p+1)), which injects an acceleration error O(dt^(p+1))
    per stage -> global O(dt^(p+1)). VV is intrinsically 2nd order so it saturates
    at predictor_order 1. The accel term buys order only for RK4/Forest-Ruth,
    which stay below their 4th order (that would need a jerk term, or the other
    packages' stage positions exchanged between workers at every stage).

    `all_accelerations` is used only when predictor_order >= 2. Callers that run a
    lower order may pass any (N,3) float64 array (e.g. zeros) as a placeholder.

    K == N reduces EXACTLY to the monolithic step at every predictor order: the
    scatter overwrites every source row, so all predictor terms vanish and the
    source set equals the target set.
    """
    src = all_positions.copy()
    if predictor_order >= 1:
        src += tau * all_velocities
    if predictor_order >= 2:
        src += (0.5 * tau * tau) * all_accelerations
    k = global_indices.shape[0]
    for i in range(k):
        gi = global_indices[i]
        if gi >= 0:
            src[gi, 0] = k_stage_positions[i, 0]
            src[gi, 1] = k_stage_positions[i, 1]
            src[gi, 2] = k_stage_positions[i, 2]
    return src
