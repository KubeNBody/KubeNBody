# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Binary format for efficient body data serialization.

Each body is a 64-byte little-endian record, with no file header:
  - n (int32): body index, 4 bytes
  - idBody (int32): body ID, 4 bytes
  - m (float64): mass, 8 bytes
  - x, y, z (float64): position, 24 bytes
  - vx, vy, vz (float64): velocity, 24 bytes

Example: 10,000 bodies = 640 KB (vs ~1MB in CSV)
"""

import struct
from typing import Dict, List, Tuple

import numpy as np


# little-endian 2 int32 + 7 float64 = 64 bytes
BODY_STRUCT = struct.Struct('<ii7d')
BODY_SIZE = 64  # bytes per body


def bodies_to_binary(bodies: List[Dict]) -> bytes:
    """Body dicts (keys n, idBody, m, x, y, z, vx, vy, vz) in the packed binary
    format."""
    n_bodies = len(bodies)
    buffer = bytearray(n_bodies * BODY_SIZE)

    for i, body in enumerate(bodies):
        BODY_STRUCT.pack_into(
            buffer, i * BODY_SIZE,
            int(body['n']),
            int(body['idBody']),
            float(body['m']),
            float(body['x']),
            float(body['y']),
            float(body['z']),
            float(body['vx']),
            float(body['vy']),
            float(body['vz'])
        )

    return bytes(buffer)


# NumPy view of one 64-byte record, field for field identical to BODY_STRUCT
# ('<ii7d', packed, little-endian), so a whole blob decodes or encodes in one
# vectorised operation instead of a Python loop over bodies.
BODY_DTYPE = np.dtype([
    ('n', '<i4'), ('idBody', '<i4'), ('m', '<f8'),
    ('pos', '<f8', (3,)), ('vel', '<f8', (3,)),
])
assert BODY_DTYPE.itemsize == BODY_SIZE == BODY_STRUCT.size


def binary_to_numpy(data: bytes, validate: bool = True) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(positions, velocities, masses, ids) of packed bodies, the worker's fast
    path: arrays as the package steppers take them (float64, ids int32),
    decoded in one vectorised pass over BODY_DTYPE. validate: ValueError for
    NaN/Inf values."""
    n_bodies = len(data) // BODY_SIZE
    rec = np.frombuffer(data, dtype=BODY_DTYPE, count=n_bodies)

    # Always copies (writable, C-contiguous, native byte order): np.frombuffer
    # views are read-only and ascontiguousarray would return one for N <= 1.
    positions = np.array(rec['pos'], dtype=np.float64, order='C', copy=True)
    velocities = np.array(rec['vel'], dtype=np.float64, order='C', copy=True)
    masses = np.array(rec['m'], dtype=np.float64, copy=True)
    ids = np.array(rec['idBody'], dtype=np.int32, copy=True)

    # Validate for NaN/Inf: report the first offending body and field
    if validate and n_bodies:
        values = np.column_stack((masses, positions, velocities))
        bad = ~np.isfinite(values)
        if bad.any():
            i, j = np.argwhere(bad)[0]
            if j == 0:
                raise ValueError(f"Body {i} has invalid mass: {values[i, 0]}")
            field_names = ['x', 'y', 'z', 'vx', 'vy', 'vz']
            raise ValueError(f"Body {i} has invalid {field_names[j - 1]}: {values[i, j]}")

    return positions, velocities, masses, ids


def numpy_to_binary(
    positions: np.ndarray,
    velocities: np.ndarray,
    masses: np.ndarray,
    ids: np.ndarray,
    start_idx: int = 0
) -> bytes:
    """K bodies' arrays in the packed binary format, their 'n' numbered from
    start_idx (a package's partial results)."""
    k = len(masses)
    # The record stores int32: refuse values that would silently wrap.
    ids_arr = np.asarray(ids)
    if k and (start_idx < -2**31 or start_idx + k - 1 >= 2**31
              or ids_arr.min() < -2**31 or ids_arr.max() >= 2**31):
        raise ValueError("body index or id outside the int32 range of the binary format")
    rec = np.empty(k, dtype=BODY_DTYPE)
    rec['n'] = np.arange(start_idx, start_idx + k, dtype=np.int64)
    rec['idBody'] = ids_arr
    rec['m'] = masses
    rec['pos'] = positions
    rec['vel'] = velocities
    return rec.tobytes()


# Parallel acceleration payload
# The acceleration-predictor distributed integrators (gvv_accel, grk4_accel,
# gfr_accel) need the epoch-start accelerations a0 of ALL bodies. Rather than
# each of the P workers recomputing a0 over all N (an O(N^2) pass per worker),
# it is published once for the committed state (the accelerations the packages
# returned, or computed) as a separate flat array, parallel to the body state
# (body i's acceleration at offset i*ACCEL_SIZE, in the same ordering as the
# bodies blob), so the 64-byte body record and the snapshot format stay as
# they are.
ACCEL_SIZE = 24  # 3 * float64 per body


def accelerations_to_binary(accelerations: np.ndarray) -> bytes:
    """Pack an (N, 3) acceleration array into a flat little-endian float64 blob."""
    arr = np.ascontiguousarray(accelerations, dtype='<f8')
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ValueError(f"accelerations must be (N, 3), got shape {arr.shape}")
    return arr.tobytes()


def binary_to_accelerations(data: bytes, n_bodies: int) -> np.ndarray:
    """Unpack a flat accel blob into a writable (n_bodies, 3) float64 array.

    Raises ValueError if the blob size doesn't match n_bodies (a stale or
    mismatched payload) — the caller should then fall back to recomputing a0.
    """
    expected = n_bodies * ACCEL_SIZE
    if len(data) != expected:
        raise ValueError(
            f"accel blob size {len(data)} != expected {expected} for {n_bodies} bodies"
        )
    return np.frombuffer(data, dtype='<f8').reshape(n_bodies, 3).copy()


