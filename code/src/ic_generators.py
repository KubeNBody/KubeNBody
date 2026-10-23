# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Reproducible, seeded initial-condition generators.

Each generator returns (positions, velocities, masses, ids) as numpy arrays in
natural units (G=1 by convention), COM-centered where noted. `to_csv` writes
them in the simulator's input schema, the format a job's input file takes.

Distributions provided:
  - plummer_sphere     : self-gravitating cluster, Aarseth (1974) sampling.
"""
from __future__ import annotations

import csv
from typing import Tuple

import numpy as np


Arrays = Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]


def _finalize(pos, vel, masses, center=True) -> Arrays:
    pos = np.ascontiguousarray(pos, dtype=np.float64)
    vel = np.ascontiguousarray(vel, dtype=np.float64)
    masses = np.ascontiguousarray(masses, dtype=np.float64)
    if center:
        total = masses.sum()
        pos = pos - (masses[:, None] * pos).sum(axis=0) / total
        vel = vel - (masses[:, None] * vel).sum(axis=0) / total
    ids = np.arange(len(masses), dtype=np.int32)
    return pos, vel, masses, ids


def plummer_sphere(n: int, total_mass: float = 1.0, scale_radius: float = 1.0,
                   G: float = 1.0, seed: int = 0) -> Arrays:
    """Sample a Plummer sphere (Aarseth, Henon & Wielen 1974).

    Positions from the Plummer density profile via inverse-CDF of the enclosed
    mass. Velocities by rejection sampling the Plummer distribution function.
    """
    rng = np.random.default_rng(seed)
    m = np.full(n, total_mass / n)

    # radii: r = a / sqrt(X^{-2/3} - 1),  X ~ U(0,1)
    x1 = rng.random(n)
    r = scale_radius / np.sqrt(x1 ** (-2.0 / 3.0) - 1.0)
    # random directions
    cth = rng.uniform(-1.0, 1.0, n)
    sth = np.sqrt(1.0 - cth * cth)
    phi = rng.uniform(0.0, 2.0 * np.pi, n)
    pos = np.stack([r * sth * np.cos(phi), r * sth * np.sin(phi), r * cth], axis=1)

    # escape speed at r: v_e = sqrt(2) * (1 + r^2/a^2)^{-1/4} * sqrt(G M / a)
    v_scale = np.sqrt(G * total_mass / scale_radius)
    v_esc = np.sqrt(2.0) * (1.0 + (r / scale_radius) ** 2) ** (-0.25) * v_scale
    # rejection sample q = v/v_esc from g(q) = q^2 (1-q^2)^{7/2}
    q = np.empty(n)
    for i in range(n):
        while True:
            x4 = rng.random()
            x5 = rng.random()
            if 0.1 * x5 < x4 * x4 * (1.0 - x4 * x4) ** 3.5:
                q[i] = x4
                break
    speed = q * v_esc
    cth = rng.uniform(-1.0, 1.0, n)
    sth = np.sqrt(1.0 - cth * cth)
    phi = rng.uniform(0.0, 2.0 * np.pi, n)
    vel = np.stack([speed * sth * np.cos(phi), speed * sth * np.sin(phi), speed * cth], axis=1)
    return _finalize(pos, vel, m)


def to_csv(path: str, pos: np.ndarray, vel: np.ndarray, masses: np.ndarray,
           ids: np.ndarray = None) -> None:
    """Write ICs in the simulator's input schema: n,idBody,m,x,y,z,vx,vy,vz."""
    n = len(masses)
    if ids is None:
        ids = np.arange(n, dtype=np.int32)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["n", "idBody", "m", "x", "y", "z", "vx", "vy", "vz"])
        for i in range(n):
            w.writerow([i + 1, int(ids[i]), masses[i],
                        pos[i, 0], pos[i, 1], pos[i, 2],
                        vel[i, 0], vel[i, 1], vel[i, 2]])
