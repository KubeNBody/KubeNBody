#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Generate the input files of the static dashboard demo's showcase jobs.

Writes, in the simulator's input
schema (n,idBody,m,x,y,z,vx,vy,vz, G=1 natural units, COM-centred):

    code/data/demo/figure_eight_3body.csv     Chenciner-Montgomery figure-eight orbit
    code/data/demo/moth_3body.csv             Suvakov-Dmitrasinovic periodic orbits:
    code/data/demo/yin_yang_3body.csv           moth I, yin-yang 1a, goggles
    code/data/demo/goggles_3body.csv
    code/data/demo/galaxy_collision.csv       two disk galaxies on a parabolic encounter

Usage:
    PYTHONPATH=code/src python -m scripts.make_demo_inputs [--out-dir code/data/demo]
"""
from __future__ import annotations

import argparse
import os

import numpy as np

import ic_generators as icg  # PYTHONPATH=code/src


# Softening the galaxy collision job runs with. The disks' circular velocities use it
GALAXY_SOFTENING_SQ = 0.0025

# Suvakov & Dmitrasinovic, PRL 110, 114301 (2013): (p1, p2) and period T for
# x1 = -x2 = (-1, 0), x3 = 0, v1 = v2 = (p1, p2), v3 = -2 (p1, p2), m = G = 1.
# Chosen for fixed-step RK4: their closest approaches (moth 0.079, yin-yang 0.013,
# goggles 0.018) still close back on themselves after two periods at the job's dt.
# Dragonfly, butterflies etc. eject a body at dt <= 1e-4 without adaptive steps.
CHOREOGRAPHIES = {
    "moth": (0.464445, 0.396060, 14.8939),
    "yin_yang": (0.513938, 0.304736, 17.3284),
    "goggles": (0.083300, 0.127889, 10.4668),
}


def figure_eight():
    """Chenciner & Montgomery (2000) choreography, period T ~= 6.3259 (G = m = 1)."""
    x1 = np.array([0.97000436, -0.24308753, 0.0])
    v3 = np.array([-0.93240737, -0.86473146, 0.0])
    pos = np.array([x1, -x1, np.zeros(3)])
    vel = np.array([-v3 / 2.0, -v3 / 2.0, v3])
    return icg._finalize(pos, vel, np.ones(3))


def suvakov(p1: float, p2: float):
    p = np.array([p1, p2, 0.0])
    pos = np.array([[-1.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
    return icg._finalize(pos, np.array([p, p, -2.0 * p]), np.ones(3))


def _disk(rng, n, core_mass, disk_mass, r_in, r_out, eps2):
    """A core plus n light bodies on circular orbits in the xy plane. Radii are
    uniform in r (not in area), so the outer rings that become tidal tails are
    well populated. Velocities balance the core plus the enclosed disk mass."""
    r = rng.uniform(r_in, r_out, n)
    phi = rng.uniform(0.0, 2.0 * np.pi, n)
    m_enc = core_mass + disk_mass * (r - r_in) / (r_out - r_in)
    vc = np.sqrt(m_enc * r ** 2 / (r ** 2 + eps2) ** 1.5)
    pos = np.stack([r * np.cos(phi), r * np.sin(phi), np.zeros(n)], axis=1)
    vel = np.stack([-vc * np.sin(phi), vc * np.cos(phi), np.zeros(n)], axis=1)
    return (np.vstack([[0.0, 0.0, 0.0], pos]), np.vstack([[0.0, 0.0, 0.0], vel]),
            np.concatenate([[core_mass], np.full(n, disk_mass / n)]))


def galaxy_collision(n_each=300, pericentre=3.2, separation=10.0, seed=7):
    """Toomre & Toomre (1972)-style prograde, coplanar, parabolic encounter of two
    disk galaxies (core mass 1, disk mass 0.1 out to radius 2.2 ~ 0.7 x pericentre)."""
    rng = np.random.default_rng(seed)
    x1, v1, m1 = _disk(rng, n_each, 1.0, 0.1, 0.4, 2.2, GALAXY_SOFTENING_SQ)
    x2, v2, m2 = _disk(rng, n_each, 1.0, 0.1, 0.4, 2.2, GALAXY_SOFTENING_SQ)
    total = m1.sum() + m2.sum()
    # Relative orbit: parabola with the given pericentre, approaching from `separation`
    p = 2.0 * pericentre
    f = -np.arccos(p / separation - 1.0)
    r = p / (1.0 + np.cos(f))
    rel_x = r * np.array([np.cos(f), np.sin(f), 0.0])
    rel_v = (total / np.sqrt(total * p)) * np.array([-np.sin(f), np.cos(f) + 1.0, 0.0])
    w1, w2 = m2.sum() / total, m1.sum() / total
    pos = np.vstack([x1 - w1 * rel_x, x2 + w2 * rel_x])
    vel = np.vstack([v1 - w1 * rel_v, v2 + w2 * rel_v])
    return icg._finalize(pos, vel, np.concatenate([m1, m2]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default=os.path.join("code", "data", "demo"))   # from the repo root
    args = parser.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    inputs = {"figure_eight_3body.csv": figure_eight()}
    for name, (p1, p2, _period) in CHOREOGRAPHIES.items():
        inputs[f"{name}_3body.csv"] = suvakov(p1, p2)
    inputs["galaxy_collision.csv"] = galaxy_collision()

    for name, (pos, vel, masses, ids) in inputs.items():
        path = os.path.join(args.out_dir, name)
        icg.to_csv(path, pos, vel, masses, ids)
        print(f"{path}: {len(masses)} bodies")


if __name__ == "__main__":
    main()
