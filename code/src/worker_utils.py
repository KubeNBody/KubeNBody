# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Shared utilities for worker processes (CPU and GPU workers).

Contains common functions for file handling and data conversion
to avoid code duplication between worker.py and worker_gpu.py.
"""

import csv
import io
from typing import Any, Dict, List, Tuple

import numpy as np

from file_service import download_file, input_csv_reader


def get_partial_results_file(bodies: List[Dict[str, Any]]) -> str:
    """The bodies as CSV text. ValueError for no bodies."""
    if not bodies:
        raise ValueError("Cannot create CSV from empty bodies list")

    with io.StringIO() as file_buffer:
        writer = csv.DictWriter(file_buffer, fieldnames=list(bodies[0].keys()))
        writer.writeheader()
        for entry in bodies:
            writer.writerow(entry)
        return file_buffer.getvalue()


def get_n_bodies(bucket: str, file_key: str) -> List[Dict[str, Any]]:
    """The bodies of a CSV object in S3, as dicts."""
    raw_file = download_file(bucket, file_key)
    return list(input_csv_reader(raw_file))


def bodies_to_numpy(
    all_bodies: List[Dict[str, Any]]
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(positions, velocities, masses, ids) arrays of body dicts (keys x, y, z,
    vx, vy, vz, m, idBody), for Numba and the GPU."""
    n = len(all_bodies)

    positions = np.zeros((n, 3), dtype=np.float64)
    velocities = np.zeros((n, 3), dtype=np.float64)
    masses = np.zeros(n, dtype=np.float64)
    ids = np.zeros(n, dtype=np.int32)

    for i, body in enumerate(all_bodies):
        positions[i, 0] = float(body['x'])
        positions[i, 1] = float(body['y'])
        positions[i, 2] = float(body['z'])

        velocities[i, 0] = float(body['vx'])
        velocities[i, 1] = float(body['vy'])
        velocities[i, 2] = float(body['vz'])

        masses[i] = float(body['m'])
        ids[i] = int(body['idBody'])

    return positions, velocities, masses, ids


def bodies_to_numpy_with_accelerations(
    all_bodies: List[Dict[str, Any]]
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, bool]:
    """bodies_to_numpy plus the accelerations (keys ax, ay, az) that velocity
    Verlet and adaptive jobs carry between steps, and whether there were any:
    an input file has none (zeros then, to be computed)."""
    n = len(all_bodies)

    positions = np.zeros((n, 3), dtype=np.float64)
    velocities = np.zeros((n, 3), dtype=np.float64)
    masses = np.zeros(n, dtype=np.float64)
    ids = np.zeros(n, dtype=np.int32)
    accelerations = np.zeros((n, 3), dtype=np.float64)

    # Input files carry no accelerations, the worker's own snapshots do
    has_accelerations = n > 0 and 'ax' in all_bodies[0]

    for i, body in enumerate(all_bodies):
        positions[i, 0] = float(body['x'])
        positions[i, 1] = float(body['y'])
        positions[i, 2] = float(body['z'])

        velocities[i, 0] = float(body['vx'])
        velocities[i, 1] = float(body['vy'])
        velocities[i, 2] = float(body['vz'])

        masses[i] = float(body['m'])
        ids[i] = int(body['idBody'])

        if has_accelerations:
            accelerations[i, 0] = float(body['ax'])
            accelerations[i, 1] = float(body['ay'])
            accelerations[i, 2] = float(body['az'])

    return positions, velocities, masses, ids, accelerations, has_accelerations


