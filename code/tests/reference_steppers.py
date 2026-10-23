# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The workers' integrators driven in-process, without a pipeline: the whole
system with the monolithic steppers, or package by package from one epoch
snapshot with the distributed ones. The tests compare the pipelines and
repeated runs against these."""
import numpy as np

import physics


METHODS = ["simple_euler", "symplectic_euler", "velocity_verlet", "rk4", "forest_ruth"]


def run_monolithic(method, pos, vel, masses, ids, dt, t_final, softening_sq=0.0):
    """Advance the whole system with our monolithic (direct O(N^2)) steppers."""
    p, v = pos.copy(), vel.copy()
    nsteps = int(round(t_final / dt))
    accel = None
    for _ in range(nsteps):
        if method == "simple_euler":
            p, v, _ = physics.step_simple_euler(p, v, masses, ids, dt, softening_sq=softening_sq)
        elif method == "symplectic_euler":
            p, v, _ = physics.step_symplectic_euler(p, v, masses, ids, dt, softening_sq=softening_sq)
        elif method == "velocity_verlet":
            if accel is None:
                accel = physics.compute_accelerations(p, masses, ids, softening_sq=softening_sq)
            p, v, accel = physics.step_velocity_verlet(p, v, masses, ids, accel, dt, softening_sq=softening_sq)
        elif method == "rk4":
            p, v, _ = physics.step_rk4(p, v, masses, ids, dt, softening_sq=softening_sq)
        elif method == "forest_ruth":
            p, v, _ = physics.step_forest_ruth(p, v, masses, ids, dt, softening_sq=softening_sq)
        else:
            raise ValueError(method)
    return p, v


def run_distributed(method, pos, vel, masses, ids, dt, t_final, softening_sq=0.0,
                    n_packages=2, predictor_order=1, accel_source="exact",
                    callback=None, callback_every=1, package_size=None):
    """Advance the system with the distributed (package) steppers: every package
    is computed from the same epoch snapshot, then merged — the real pattern.

    predictor_order selects the background predictor for the multi-stage steppers
    (0 frozen / 1 linear / 2 accel). accel_source selects where the accel
    predictor's full-system epoch-start accelerations come from:
      "exact"    — recomputed from the epoch-start state every step (a serial
                   O(N^2) pass).
      "returned" — assembled from the last-stage accelerations that each package
                   returned with its previous step (the pipeline's default: no
                   extra force evaluation, the first step uses the exact value).
      "differenced" — (v_n - v_{n-1}) / dt from the last two merged states, the
                   co-simulation approach of extrapolating from past
                   communication points (O(dt) accurate, no
                   force evaluation, the first step uses the exact value).
    For velocity Verlet with the accel predictor the package rows of that array
    also serve as its cached acceleration (one force evaluation per task).
    callback(step, p, v) is called every callback_every steps (and at step 0).
    """
    if accel_source not in ("exact", "returned", "differenced"):
        raise ValueError(accel_source)
    p, v = pos.copy(), vel.copy()
    n = len(masses)
    if package_size:
        # the pipeline's slicing: fixed-size packages, the last one shorter
        bounds = [(a, min(a + package_size, n)) for a in range(0, n, package_size)]
    else:
        bounds = [(i * n // n_packages, (i + 1) * n // n_packages) for i in range(n_packages)]
    bounds = [(a, b) for a, b in bounds if b > a]
    nsteps = int(round(t_final / dt))
    returned = None
    prev_v = None
    if callback is not None:
        callback(0, p, v)
    for step in range(1, nsteps + 1):
        new_p, new_v = np.empty_like(p), np.empty_like(v)
        new_a = np.empty_like(p)
        # Full-system epoch-start accelerations for the accel predictor (order 2).
        # The multi-stage steppers require an (N,3) array regardless, so a zeros
        # placeholder serves the frozen/linear paths.
        if predictor_order >= 2:
            if accel_source == "returned" and returned is not None:
                a_all = returned
            elif accel_source == "differenced" and prev_v is not None:
                a_all = np.ascontiguousarray((v - prev_v) / dt)
            else:
                a_all = physics.compute_accelerations(p, masses, ids, softening_sq=softening_sq)
        else:
            a_all = np.zeros_like(p)
        for (a, b) in bounds:
            kp, kv, km, kid = p[a:b], v[a:b], masses[a:b], ids[a:b]
            if method == "simple_euler":
                np_, nv_, na_ = physics.step_package_simple_euler(kp, kv, km, kid, p, masses, ids, dt, softening_sq=softening_sq)
            elif method == "symplectic_euler":
                np_, nv_, na_ = physics.step_package_symplectic_euler(kp, kv, km, kid, p, masses, ids, dt, softening_sq=softening_sq)
            elif method == "velocity_verlet":
                if predictor_order >= 2:
                    ka = np.ascontiguousarray(a_all[a:b])
                else:
                    ka = physics.compute_package_accelerations(kp, kid, p, masses, ids, softening_sq=softening_sq)
                np_, nv_, na_ = physics.step_package_velocity_verlet(kp, kv, km, kid, ka, p, v, a_all, masses, ids, dt, predictor_order=predictor_order, softening_sq=softening_sq)
            elif method == "rk4":
                np_, nv_, na_ = physics.step_package_rk4(kp, kv, km, kid, p, v, a_all, masses, ids, dt, predictor_order=predictor_order, softening_sq=softening_sq)
            elif method == "forest_ruth":
                np_, nv_, na_ = physics.step_package_forest_ruth(kp, kv, km, kid, p, v, a_all, masses, ids, dt, predictor_order=predictor_order, softening_sq=softening_sq)
            else:
                raise ValueError(method)
            new_p[a:b], new_v[a:b], new_a[a:b] = np_, nv_, na_
        prev_v = v
        p, v, returned = new_p, new_v, new_a
        if callback is not None and step % callback_every == 0:
            callback(step, p, v)
    return p, v
