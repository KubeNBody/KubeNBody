# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""CPUBackend: the monolithic velocity Verlet step against the distributed
gvv_accel package step, and the JIT warm-up."""
from __future__ import annotations

import os
import subprocess
import sys

import numpy as np
import pytest

from barnes_hut import compute_accelerations_barnes_hut
from helpers import random_system
from physics import compute_accelerations
from workers.backends.cpu_backend import CPUBackend
from workers.base.compute_backend import ComputeError


@pytest.mark.parametrize("algorithm", ["barnes_hut", "direct"])
def test_monolithic_vv_initial_accel_uses_job_softening(algorithm):
    """compute_step without cached accelerations derives a(0) itself. It used
    the default SOFTENING_SQ = 1e-9 for Barnes-Hut instead of the job's."""
    pos, vel, m, ids = random_system()
    eps2, dt, theta = 1.0, 0.01, 0.5
    b = CPUBackend()
    if algorithm == "barnes_hut":
        a0 = compute_accelerations_barnes_hut(pos, m, ids, theta, 1.0, eps2)
    else:
        a0 = compute_accelerations(pos, m, ids, softening_sq=eps2)
    implicit = b.compute_step(pos, vel, m, ids, dt, algorithm, "velocity_verlet", theta, eps2)
    explicit = b.compute_step(pos, vel, m, ids, dt, algorithm, "velocity_verlet", theta, eps2,
                              previous_accelerations=a0)
    for x, y in zip(implicit, explicit):
        assert np.array_equal(x, y)


def test_monolithic_vv_bh_step_matches_distributed_gvv_accel():
    """One monolithic VV+BH step equals the distributed gvv_accel+BH step of a
    single package covering every body (whose a(0) is the softened BH one)."""
    pos, vel, m, ids = random_system()
    eps2, dt, theta = 1.0, 0.01, 0.5
    b = CPUBackend()
    p1, v1, a1 = b.compute_step(pos, vel, m, ids, dt, "barnes_hut", "velocity_verlet", theta, eps2)
    p2, v2, a2 = b.compute_package_step(
        pos.copy(), vel.copy(), m.copy(), ids.copy(), pos, vel, m, ids, dt,
        algorithm="barnes_hut", integration_method="gvv_accel",
        barnes_hut_theta=theta, softening_sq=eps2,
    )
    np.testing.assert_allclose(p1, p2, rtol=0, atol=1e-14)
    np.testing.assert_allclose(v1, v2, rtol=0, atol=1e-14)
    np.testing.assert_allclose(a1, a2, rtol=0, atol=1e-12)


# Distributed velocity Verlet with the acceleration predictor is bitwise the
# monolithic run also for the Barnes-Hut tree, over several packages and steps.
def test_gvv_accel_tree_is_bitwise_monolithic_over_packages_and_steps():
    rng = np.random.default_rng(2)
    n, n_packages, dt, theta = 200, 4, 0.01, 0.5
    pos, vel = rng.normal(size=(n, 3)), 0.1 * rng.normal(size=(n, 3))
    m, ids = np.full(n, 1.0 / n), np.arange(n, dtype=np.int64)
    b = CPUBackend()
    for eps2 in (1.0, 1e-3):
        pm, vm, am = pos.copy(), vel.copy(), None
        pd, vd, ad = pos.copy(), vel.copy(), None
        ps = -(-n // n_packages)
        for _ in range(5):
            pm, vm, am = b.compute_step(pm, vm, m, ids, dt, "barnes_hut", "velocity_verlet",
                                        theta, eps2, previous_accelerations=am)
            parts = []
            for k in range(n_packages):
                sl = slice(k * ps, min((k + 1) * ps, n))
                parts.append(b.compute_package_step(
                    pd[sl].copy(), vd[sl].copy(), m[sl].copy(), ids[sl].copy(), pd, vd, m, ids, dt,
                    algorithm="barnes_hut", integration_method="gvv_accel",
                    barnes_hut_theta=theta, softening_sq=eps2, all_accelerations=ad))
            pd, vd, ad = (np.vstack([p[i] for p in parts]) for i in range(3))
        assert np.array_equal(pm, pd) and np.array_equal(vm, vd) and np.array_equal(am, ad)


def test_cpu_backend_warmup_compiles_the_direct_summation_kernels():
    """The warm-up compiles every entry point the backend calls, so e.g.
    physics.step_package_rk4 does not compile in the first epoch. Run in a
    subprocess so earlier tests' compilations do not count."""
    names = ("step_direct", "step_velocity_verlet", "step_rk4", "step_forest_ruth", "step_package_direct",
             "step_package_velocity_verlet", "step_package_rk4", "step_package_forest_ruth",
             "accelerations_of", "package_accelerations_of")
    code = ("from workers.backends.cpu_backend import CPUBackend; import physics as P; CPUBackend().warmup(); "
            f"print(*(len(getattr(P, n).signatures) for n in {names!r}))")
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=600,
                         env={**os.environ, "PYTHONPATH": src}, cwd=src)
    assert out.returncode == 0, out.stderr[-2000:]
    counts = dict(zip(names, (int(x) for x in out.stdout.split()[-len(names):])))
    assert all(c >= 1 for c in counts.values()), counts


def test_absent_package_ids_are_an_error_not_the_last_row():
    """The accel-predictor lookup's -1 ("absent") indexed the last row."""
    rng = np.random.default_rng(0)
    p, v, m = rng.normal(size=(6, 3)), rng.normal(size=(6, 3)), np.ones(6)
    ids = np.arange(6, dtype=np.int32)
    k_ids = np.array([0, 99], dtype=np.int32)              # 99 is not in the system
    with pytest.raises(ComputeError):
        CPUBackend().compute_package_step(p[:2].copy(), v[:2].copy(), m[:2].copy(), k_ids, p, v, m, ids,
                                          0.01, integration_method="gvv_accel", softening_sq=1e-2,
                                          all_accelerations=np.zeros((6, 3)))


@pytest.mark.parametrize("package", [False, True])
def test_unknown_integration_method_is_an_error_not_simple_euler(package):
    """An unknown method name fell through to simple Euler (the GPU engine raises)."""
    pos, vel, m, ids = random_system()
    b = CPUBackend()
    with pytest.raises(ValueError, match="Unknown integration_method"):
        if package:
            b.compute_package_step(pos[:3].copy(), vel[:3].copy(), m[:3].copy(), ids[:3].copy(),
                                   pos, vel, m, ids, 0.01, integration_method="leapfrog")
        else:
            b.compute_step(pos, vel, m, ids, 0.01, integration_method="leapfrog")
