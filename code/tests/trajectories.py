# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Reference trajectories: a fixed small cluster stepped through every
integrator, force algorithm and engine the workers use, monolithic steps and
package steps (every predictor), through the compute-backend interface.

The recorded final states are compared bit for bit by
test_trajectory_reference.py. Numba compiles with fast-math for the host CPU
and the GPU kernels for the device, so a recording holds for one CPU model
(or one GPU) only: each gets its own file, and a machine without one skips.

Record (from code/):
    PYTHONPATH=src:tests python -m trajectories          # this CPU
    PYTHONPATH=src:tests python -m trajectories --gpu    # this GPU
Record again only for a change that is meant to alter the trajectories.
"""
import argparse
import os
import platform
import re

import llvmlite.binding as llvm
import numpy as np

from workers.backends.cpu_backend import CPUBackend
from workers.backends.gpu_backend import GPUBackend, get_gpu_info


DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trajectories")
N, PACKAGES, STEPS, DT, SOFTENING_SQ, THETA = 40, 3, 25, 2e-3, 0.05, 0.5

MONOLITHIC_METHODS = ("simple_euler", "symplectic_euler", "velocity_verlet", "rk4", "forest_ruth")
PACKAGE_METHODS = ("simple_euler", "symplectic_euler",
                   "gvv_frozen", "gvv_linear", "gvv_accel",
                   "grk4_frozen", "grk4_linear", "grk4_accel",
                   "gfr_frozen", "gfr_linear", "gfr_accel")


def initial_state():
    """A softened cluster of N bodies with unequal masses, the same on every machine."""
    rng = np.random.default_rng(2026)
    positions = rng.standard_normal((N, 3))
    velocities = 0.3 * rng.standard_normal((N, 3))
    masses = rng.uniform(0.5, 1.5, N)
    ids = np.arange(1, N + 1, dtype=np.int32)
    return positions, velocities, masses, ids


def _slices():
    size = -(-N // PACKAGES)
    return [slice(s, min(s + size, N)) for s in range(0, N, size)]


def run_monolithic(backend, method, algorithm):
    """STEPS monolithic steps, as the monolithic worker takes them."""
    pos, vel, masses, ids = initial_state()
    acc = None
    for _ in range(STEPS):
        pos, vel, acc = backend.compute_step(pos, vel, masses, ids, DT, algorithm=algorithm,
                                             integration_method=method, barnes_hut_theta=THETA,
                                             softening_sq=SOFTENING_SQ, previous_accelerations=acc)
    return pos, vel, acc


def run_resident(backend, method):
    """STEPS steps on the backend's resident state (the GPU monolithic path)."""
    pos, vel, masses, ids = initial_state()
    backend.load_resident_state(pos, vel, masses, ids, DT, integration_method=method,
                                softening_sq=SOFTENING_SQ)
    for _ in range(STEPS):
        backend.step_resident(DT)
    return backend.fetch_resident_state()


def run_packages(backend, method, algorithm, returned):
    """STEPS epochs of package steps, as the distributed workers take them:
    every package from the epoch-start state of all bodies. returned: the
    accel predictor's a(0) is the packages' own last-stage accelerations of
    the previous epoch (the pipeline's returned rows), else the backend
    computes it."""
    pos, vel, masses, ids = initial_state()
    acc = None
    for _ in range(STEPS):
        new_pos, new_vel, new_acc = np.empty_like(pos), np.empty_like(vel), np.empty_like(pos)
        for s in _slices():
            new_pos[s], new_vel[s], new_acc[s] = backend.compute_package_step(
                pos[s], vel[s], masses[s], ids[s], pos, vel, masses, ids, DT, algorithm=algorithm,
                integration_method=method, barnes_hut_theta=THETA, softening_sq=SOFTENING_SQ,
                all_accelerations=acc if returned else None)
        pos, vel, acc = new_pos, new_vel, new_acc
    return pos, vel, acc


def cpu_cases():
    """{case name: (positions, velocities, accelerations)} for the CPU backend."""
    backend = CPUBackend()
    out = {}
    for algorithm in ("direct", "barnes_hut"):
        for method in MONOLITHIC_METHODS:
            out[f"monolithic/{algorithm}/{method}"] = run_monolithic(backend, method, algorithm)
        for method in PACKAGE_METHODS:
            out[f"package/{algorithm}/{method}"] = run_packages(backend, method, algorithm, returned=False)
            if method.endswith("_accel"):
                out[f"package/{algorithm}/{method}/returned"] = run_packages(backend, method, algorithm,
                                                                           returned=True)
    return out


def gpu_cases():
    """The same for the GPU backend (direct summation only)."""
    backend = GPUBackend()
    out = {}
    try:
        for method in MONOLITHIC_METHODS:
            out[f"monolithic/direct/{method}"] = run_monolithic(backend, method, "direct")
            out[f"resident/direct/{method}"] = run_resident(backend, method)
        for method in PACKAGE_METHODS:
            out[f"package/direct/{method}"] = run_packages(backend, method, "direct", returned=False)
            if method.endswith("_accel"):
                out[f"package/direct/{method}/returned"] = run_packages(backend, method, "direct", returned=True)
    finally:
        backend.cleanup()
    return out


def _slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()


def cpu_key():
    return f"cpu-{_slug(platform.machine())}-{_slug(llvm.get_host_cpu_name())}"


def gpu_key():
    return f"gpu-{_slug(get_gpu_info()['name'])}"


def path_for(key):
    return os.path.join(DATA, f"{key}.npz")


def save(key, cases):
    os.makedirs(DATA, exist_ok=True)
    arrays = {f"{name}/{part}": a for name, state in cases.items()
              for part, a in zip(("positions", "velocities", "accelerations"), state)}
    np.savez_compressed(path_for(key), **arrays)
    return path_for(key)


def load(key):
    with np.load(path_for(key)) as f:
        arrays = dict(f)
    cases = {}
    for name in {k.rsplit("/", 1)[0] for k in arrays}:
        cases[name] = tuple(arrays[f"{name}/{part}"] for part in ("positions", "velocities", "accelerations"))
    return cases


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gpu", action="store_true", help="record this GPU's trajectories instead of this CPU's")
    a = ap.parse_args()
    key, cases = (gpu_key(), gpu_cases()) if a.gpu else (cpu_key(), cpu_cases())
    print(f"recorded {len(cases)} trajectories to {save(key, cases)}")


if __name__ == "__main__":
    main()
