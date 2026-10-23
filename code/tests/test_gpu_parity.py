# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""CPU vs GPU parity tests across all integration methods.

Verifies that the GPU backend produces results matching the CPU backend
(within float tolerance) for every supported integration method, in both
monolithic and single-package modes. Skips cleanly when PyCUDA or a
CUDA-capable GPU is unavailable.

These tests deliberately depend only on the physics modules (no worker /
ORM imports) so they can run in a fresh GPU container without bringing
up Postgres / RabbitMQ.
"""
from __future__ import annotations

import numpy as np
import pytest

from constants import G_CONST
from workers.backends.cpu_backend import CPUBackend
from workers.backends.gpu_backend import GPUBackend


pycuda = pytest.importorskip("pycuda")  # noqa: F841 — imported for skip side effect

from physics_gpu import GPUNBodyEngine, is_gpu_available  # noqa: E402


if not is_gpu_available():
    pytest.skip("No CUDA-capable GPU available", allow_module_level=True)

import physics  # noqa: E402


INTEGRATION_METHODS = [
    "simple_euler",
    "symplectic_euler",
    "velocity_verlet",
    "rk4",
    "forest_ruth",
]


# =============================================================================
# Initial conditions
# =============================================================================

def _make_two_body():
    """Two-body system: large central + small orbiter, COM-centered."""
    m1, m2 = 1.0, 1e-3
    r = 1.0
    v = np.sqrt((m1 + m2) / r)
    positions = np.array([[0.0, 0.0, 0.0], [r, 0.0, 0.0]], dtype=np.float64)
    velocities = np.array([[0.0, 0.0, 0.0], [0.0, v, 0.0]], dtype=np.float64)
    masses = np.array([m1, m2], dtype=np.float64)
    ids = np.array([0, 1], dtype=np.int32)
    total = m1 + m2
    positions -= (masses[:, None] * positions).sum(axis=0) / total
    velocities -= (masses[:, None] * velocities).sum(axis=0) / total
    return positions, velocities, masses, ids


def _make_small_cluster(n: int = 8, seed: int = 7):
    rng = np.random.default_rng(seed)
    positions = rng.normal(scale=0.5, size=(n, 3)).astype(np.float64)
    velocities = rng.normal(scale=0.1, size=(n, 3)).astype(np.float64)
    masses = np.full(n, 1.0 / n, dtype=np.float64)
    ids = np.arange(n, dtype=np.int32)
    total = masses.sum()
    positions -= (masses[:, None] * positions).sum(axis=0) / total
    velocities -= (masses[:, None] * velocities).sum(axis=0) / total
    return positions, velocities, masses, ids


# =============================================================================
# CPU step drivers (one step at a time, mirroring backend dispatch logic)
# =============================================================================

def _cpu_step_monolithic(method, pos, vel, masses, ids, dt, accel_cache, eps2):
    """Returns (new_pos, new_vel, new_accel_cache_for_VV)."""
    if method == "simple_euler":
        p, v, _ = physics.step_simple_euler(pos, vel, masses, ids, dt, softening_sq=eps2)
        return p, v, None
    if method == "symplectic_euler":
        p, v, _ = physics.step_symplectic_euler(pos, vel, masses, ids, dt, softening_sq=eps2)
        return p, v, None
    if method == "velocity_verlet":
        if accel_cache is None:
            accel_cache = physics.compute_accelerations(pos, masses, ids, softening_sq=eps2)
        p, v, new_accel = physics.step_velocity_verlet(
            pos, vel, masses, ids, accel_cache, dt, softening_sq=eps2,
        )
        return p, v, new_accel
    if method == "rk4":
        p, v, _ = physics.step_rk4(pos, vel, masses, ids, dt, softening_sq=eps2)
        return p, v, None
    if method == "forest_ruth":
        p, v, _ = physics.step_forest_ruth(pos, vel, masses, ids, dt, softening_sq=eps2)
        return p, v, None
    raise ValueError(method)


def _cpu_run_monolithic(method, pos, vel, masses, ids, dt, n_steps, eps2):
    p, v = pos.copy(), vel.copy()
    accel_cache = None
    for _ in range(n_steps):
        p, v, accel_cache = _cpu_step_monolithic(method, p, v, masses, ids, dt, accel_cache, eps2)
    return p, v


def _cpu_step_package(method, kp, kv, km, kids, ap, av, am, aids, dt, eps2):
    """Single package step. For K==N this equals one monolithic step.

    The step_package_* functions return (pos, vel, accel), and accel is
    dropped here for the parity comparison. `av` is all_velocities, used by
    the multi-stage integrators for the drift-corrected background.
    """
    # Epoch-start accelerations of ALL bodies, for the multi-stage steppers'
    # background predictor. GPU parity uses the linear predictor (order 1), which
    # ignores this, but the steppers still require the (N,3) argument.
    aa = physics.compute_accelerations(ap, am, aids, softening_sq=eps2)
    if method == "simple_euler":
        p, v, _ = physics.step_package_simple_euler(kp, kv, km, kids, ap, am, aids, dt, softening_sq=eps2)
        return p, v
    if method == "symplectic_euler":
        p, v, _ = physics.step_package_symplectic_euler(kp, kv, km, kids, ap, am, aids, dt, softening_sq=eps2)
        return p, v
    if method == "velocity_verlet":
        k_accel = physics.compute_package_accelerations(kp, kids, ap, am, aids, softening_sq=eps2)
        p, v, _ = physics.step_package_velocity_verlet(
            kp, kv, km, kids, k_accel, ap, av, aa, am, aids, dt, softening_sq=eps2,
        )
        return p, v
    if method == "rk4":
        p, v, _ = physics.step_package_rk4(kp, kv, km, kids, ap, av, aa, am, aids, dt, softening_sq=eps2)
        return p, v
    if method == "forest_ruth":
        p, v, _ = physics.step_package_forest_ruth(kp, kv, km, kids, ap, av, aa, am, aids, dt, softening_sq=eps2)
        return p, v
    raise ValueError(method)


def _cpu_run_package(method, pos, vel, masses, ids, dt, n_steps, eps2):
    """K==N package mode — package is the whole system."""
    p, v = pos.copy(), vel.copy()
    for _ in range(n_steps):
        p, v = _cpu_step_package(method, p, v, masses, ids, p, v, masses, ids, dt, eps2)
    return p, v


# =============================================================================
# GPU step drivers
# =============================================================================

def _bodies_dict(pos, vel, masses, ids):
    return [
        {'idBody': int(ids[i]), 'm': float(masses[i]),
         'x': float(pos[i, 0]), 'y': float(pos[i, 1]), 'z': float(pos[i, 2]),
         'vx': float(vel[i, 0]), 'vy': float(vel[i, 1]), 'vz': float(vel[i, 2])}
        for i in range(len(ids))
    ]


def _gpu_run_monolithic(method, pos, vel, masses, ids, dt, n_steps, eps2):
    engine = GPUNBodyEngine(
        bodies=_bodies_dict(pos, vel, masses, ids),
        time_step=dt,
        integration_method=method,
        softening_squared=eps2,
        gravitational_constant=1.0,
    )
    try:
        for _ in range(n_steps):
            engine.simulate_single_step()
        return engine.get_state_arrays()
    finally:
        engine.cleanup()


def _gpu_run_package_full(method, pos, vel, masses, ids, dt, n_steps, eps2):
    """K==N package mode through GPUNBodyEngine.simulate_package_step."""
    engine = GPUNBodyEngine(
        bodies=_bodies_dict(pos, vel, masses, ids),
        time_step=dt,
        integration_method=method,
        softening_squared=eps2,
        gravitational_constant=1.0,
    )
    try:
        for _ in range(n_steps):
            engine.simulate_package_step(0, len(ids))
        return engine.get_state_arrays()
    finally:
        engine.cleanup()


# =============================================================================
# Parity tests
# =============================================================================

@pytest.mark.parametrize("method", INTEGRATION_METHODS)
def test_monolithic_parity_two_body(method):
    pos, vel, masses, ids = _make_two_body()
    dt = 1e-3
    n_steps = 50
    eps2 = 1e-9

    cpu_p, cpu_v = _cpu_run_monolithic(method, pos, vel, masses, ids, dt, n_steps, eps2)
    gpu_p, gpu_v = _gpu_run_monolithic(method, pos, vel, masses, ids, dt, n_steps, eps2)

    np.testing.assert_allclose(gpu_p, cpu_p, atol=1e-10, rtol=1e-9,
                               err_msg=f"{method}: monolithic position mismatch")
    np.testing.assert_allclose(gpu_v, cpu_v, atol=1e-10, rtol=1e-9,
                               err_msg=f"{method}: monolithic velocity mismatch")


@pytest.mark.parametrize("method", INTEGRATION_METHODS)
def test_monolithic_parity_small_cluster(method):
    pos, vel, masses, ids = _make_small_cluster()
    dt = 5e-4
    n_steps = 30
    eps2 = 1e-6

    cpu_p, cpu_v = _cpu_run_monolithic(method, pos, vel, masses, ids, dt, n_steps, eps2)
    gpu_p, gpu_v = _gpu_run_monolithic(method, pos, vel, masses, ids, dt, n_steps, eps2)

    np.testing.assert_allclose(gpu_p, cpu_p, atol=1e-9, rtol=1e-8,
                               err_msg=f"{method}: cluster monolithic position mismatch")
    np.testing.assert_allclose(gpu_v, cpu_v, atol=1e-9, rtol=1e-8,
                               err_msg=f"{method}: cluster monolithic velocity mismatch")


@pytest.mark.parametrize("method", INTEGRATION_METHODS)
def test_package_parity_full_system(method):
    """GPU package mode (K==N) must match CPU package mode for every method.

    Exercises the frozen-source path on the GPU and confirms it matches
    CPU step_package_*.
    """
    pos, vel, masses, ids = _make_small_cluster(n=8, seed=11)
    dt = 5e-4
    n_steps = 20
    eps2 = 1e-6

    cpu_p, cpu_v = _cpu_run_package(method, pos, vel, masses, ids, dt, n_steps, eps2)
    gpu_p, gpu_v = _gpu_run_package_full(method, pos, vel, masses, ids, dt, n_steps, eps2)

    np.testing.assert_allclose(gpu_p, cpu_p, atol=1e-9, rtol=1e-8,
                               err_msg=f"{method}: package position mismatch")
    np.testing.assert_allclose(gpu_v, cpu_v, atol=1e-9, rtol=1e-8,
                               err_msg=f"{method}: package velocity mismatch")


@pytest.mark.parametrize("method", INTEGRATION_METHODS)
def test_package_parity_sub_n(method):
    """K<N parity: only a sub-slice of the system is updated, the rest stay
    frozen as gravitational sources. This is the scenario the frozen-source
    refactor was actually designed for — and the K==N test above misses it.

    Setup: N=12 bodies. Update package [start=4, K=4] (the middle slice).
    The first 4 and last 4 bodies are source-only — their positions/velocities
    must NOT change across the step.
    """
    pos, vel, masses, ids = _make_small_cluster(n=12, seed=42)
    start_idx = 4
    K = 4
    dt = 5e-4
    eps2 = 1e-6

    # --- CPU: single package step over the middle slice -------------------
    kp = pos[start_idx:start_idx + K].copy()
    kv = vel[start_idx:start_idx + K].copy()
    km = masses[start_idx:start_idx + K].copy()
    kids = ids[start_idx:start_idx + K].copy()

    cpu_kp, cpu_kv = _cpu_step_package(
        method, kp, kv, km, kids, pos.copy(), vel, masses, ids, dt, eps2,
    )

    # --- GPU: same setup, all N loaded, but only the slice runs -----------
    engine = GPUNBodyEngine(
        bodies=_bodies_dict(pos, vel, masses, ids),
        time_step=dt, integration_method=method,
        softening_squared=eps2, gravitational_constant=1.0,
    )
    try:
        engine.simulate_package_step(start_idx, K)
        gpu_pos_all, gpu_vel_all = engine.get_state_arrays()
    finally:
        engine.cleanup()

    # Updated slice must match CPU within tolerance.
    np.testing.assert_allclose(
        gpu_pos_all[start_idx:start_idx + K], cpu_kp,
        atol=1e-10, rtol=1e-9,
        err_msg=f"{method}: sub-N package position mismatch CPU vs GPU",
    )
    np.testing.assert_allclose(
        gpu_vel_all[start_idx:start_idx + K], cpu_kv,
        atol=1e-10, rtol=1e-9,
        err_msg=f"{method}: sub-N package velocity mismatch CPU vs GPU",
    )

    # Frozen-source invariant: non-package bodies must not have moved at all.
    # (This catches any bug that accidentally mutates gpu_bodies for
    # source-only bodies during intermediate stages.)
    non_pkg_idx = np.array(
        [i for i in range(len(ids)) if not (start_idx <= i < start_idx + K)]
    )
    np.testing.assert_array_equal(
        gpu_pos_all[non_pkg_idx], pos[non_pkg_idx],
        err_msg=f"{method}: non-package GPU positions changed (frozen-source violation)",
    )
    np.testing.assert_array_equal(
        gpu_vel_all[non_pkg_idx], vel[non_pkg_idx],
        err_msg=f"{method}: non-package GPU velocities changed (frozen-source violation)",
    )


@pytest.mark.parametrize("method", ["rk4", "forest_ruth"])
def test_package_accel_predictor_parity_sub_n(method):
    """Accel predictor (order 2) sub-N parity: the GPU tile kernel's
    all_source_accelerations path (background += 0.5*tau^2*a0) must match CPU
    step_package_*(predictor_order=2). a0 is uploaded via set_source_accelerations.

    This is the GPU verification of the acceleration predictor (grk4_accel /
    gfr_accel). Only RK4/Forest-Ruth (VV saturates at order 2, no accel variant).
    """
    pos, vel, masses, ids = _make_small_cluster(n=12, seed=42)
    start_idx, K = 4, 4
    dt, eps2 = 5e-4, 1e-6

    # Full-system epoch-start accelerations (what the merger ships / worker uploads)
    a0 = physics.compute_accelerations(pos, masses, ids, softening_sq=eps2)

    # --- CPU: single accel-predictor package step over the middle slice ---
    kp = pos[start_idx:start_idx + K].copy()
    kv = vel[start_idx:start_idx + K].copy()
    km = masses[start_idx:start_idx + K].copy()
    kids = ids[start_idx:start_idx + K].copy()
    stepper = physics.step_package_rk4 if method == "rk4" else physics.step_package_forest_ruth
    cpu_kp, cpu_kv, _ = stepper(
        kp, kv, km, kids, pos.copy(), vel, a0, masses, ids, dt,
        predictor_order=2, softening_sq=eps2,
    )

    # --- GPU: upload a0, run the same slice with predictor_order=2 ---------
    engine = GPUNBodyEngine(
        bodies=_bodies_dict(pos, vel, masses, ids),
        time_step=dt, integration_method=method,
        softening_squared=eps2, gravitational_constant=1.0,
    )
    try:
        engine.set_source_accelerations(a0)
        engine.simulate_package_step(start_idx, K, predictor_order=2)
        gpu_pos_all, gpu_vel_all = engine.get_state_arrays()
    finally:
        engine.cleanup()
    np.testing.assert_allclose(
        gpu_pos_all[start_idx:start_idx + K], cpu_kp, atol=1e-10, rtol=1e-9,
        err_msg=f"{method}: accel-predictor sub-N position mismatch CPU vs GPU",
    )
    np.testing.assert_allclose(
        gpu_vel_all[start_idx:start_idx + K], cpu_kv, atol=1e-10, rtol=1e-9,
        err_msg=f"{method}: accel-predictor sub-N velocity mismatch CPU vs GPU",
    )
    # Non-package bodies must remain untouched (only reads changed, not writes).
    non_pkg = np.array([i for i in range(len(ids)) if not (start_idx <= i < start_idx + K)])
    np.testing.assert_array_equal(gpu_pos_all[non_pkg], pos[non_pkg])
    np.testing.assert_array_equal(gpu_vel_all[non_pkg], vel[non_pkg])


def test_package_vv_accel_uses_source_rows_sub_n():
    """gvv_accel on GPU: with the acceleration predictor the package's old
    acceleration is taken from its rows of the uploaded a0 (one force
    evaluation per task), and the step matches CPU step_package_velocity_verlet
    fed the same rows, and the monolithic velocity Verlet step (the scheme is
    exact for VV)."""
    pos, vel, masses, ids = _make_small_cluster(n=12, seed=7)
    start_idx, K = 4, 4
    dt, eps2 = 5e-4, 1e-6
    a0 = physics.compute_accelerations(pos, masses, ids, softening_sq=eps2)
    sl = slice(start_idx, start_idx + K)

    cpu_kp, cpu_kv, cpu_ka = physics.step_package_velocity_verlet(
        pos[sl].copy(), vel[sl].copy(), masses[sl].copy(), ids[sl].copy(),
        np.ascontiguousarray(a0[sl]), pos.copy(), vel, a0, masses, ids, dt,
        predictor_order=2, softening_sq=eps2,
    )
    mono_p, mono_v, _ = physics.step_velocity_verlet(pos, vel, masses, ids, a0, dt, softening_sq=eps2)
    np.testing.assert_array_equal(cpu_kp, mono_p[sl])   # exactness on CPU (reference)

    engine = GPUNBodyEngine(
        bodies=_bodies_dict(pos, vel, masses, ids),
        time_step=dt, integration_method="velocity_verlet",
        softening_squared=eps2, gravitational_constant=1.0,
    )
    try:
        engine.set_source_accelerations(a0)
        _, _, gpu_ka = engine.simulate_package_step(start_idx, K, predictor_order=2)
        gpu_pos_all, gpu_vel_all = engine.get_state_arrays()
    finally:
        engine.cleanup()
    np.testing.assert_allclose(gpu_pos_all[sl], cpu_kp, atol=1e-10, rtol=1e-9)
    np.testing.assert_allclose(gpu_vel_all[sl], cpu_kv, atol=1e-10, rtol=1e-9)
    np.testing.assert_allclose(gpu_ka, cpu_ka, atol=1e-10, rtol=1e-8)


@pytest.mark.parametrize("method", INTEGRATION_METHODS)
def test_package_accel_return_parity(method):
    """compute_package_step's returned accelerations must match between
    CPU and GPU: distributed_worker takes the adaptive-dt a_max from them, so
    a divergence here would corrupt timestep selection.
    """
    pos, vel, masses, ids = _make_small_cluster(n=8, seed=11)
    dt = 5e-4
    eps2 = 1e-6

    # CPU path: call the underlying physics functions and grab the 3rd value.
    if method == "simple_euler":
        _, _, cpu_accel = physics.step_package_simple_euler(
            pos, vel, masses, ids, pos, masses, ids, dt, softening_sq=eps2)
    elif method == "symplectic_euler":
        _, _, cpu_accel = physics.step_package_symplectic_euler(
            pos, vel, masses, ids, pos, masses, ids, dt, softening_sq=eps2)
    elif method == "velocity_verlet":
        aa = physics.compute_accelerations(pos, masses, ids, softening_sq=eps2)
        k_accel = physics.compute_package_accelerations(
            pos, ids, pos, masses, ids, softening_sq=eps2)
        _, _, cpu_accel = physics.step_package_velocity_verlet(
            pos, vel, masses, ids, k_accel, pos, vel, aa, masses, ids, dt, softening_sq=eps2)
    elif method == "rk4":
        aa = physics.compute_accelerations(pos, masses, ids, softening_sq=eps2)
        _, _, cpu_accel = physics.step_package_rk4(
            pos, vel, masses, ids, pos, vel, aa, masses, ids, dt, softening_sq=eps2)
    elif method == "forest_ruth":
        aa = physics.compute_accelerations(pos, masses, ids, softening_sq=eps2)
        _, _, cpu_accel = physics.step_package_forest_ruth(
            pos, vel, masses, ids, pos, vel, aa, masses, ids, dt, softening_sq=eps2)
    else:
        raise AssertionError(method)

    # GPU path: single step, capture the returned accel array.
    engine = GPUNBodyEngine(
        bodies=_bodies_dict(pos, vel, masses, ids),
        time_step=dt, integration_method=method,
        softening_squared=eps2, gravitational_constant=1.0,
    )
    try:
        _, _, gpu_accel = engine.simulate_package_step(0, len(ids))
    finally:
        engine.cleanup()

    np.testing.assert_allclose(
        gpu_accel, cpu_accel, atol=1e-9, rtol=1e-8,
        err_msg=f"{method}: package accel return mismatch (used for adaptive dt)",
    )


def test_rk4_evaluates_at_correct_intermediate_positions():
    """GPU RK4 evaluates stages 3 and 4 at the right positions: for an
    asymmetric configuration, where RK4 differs noticeably from Euler, it
    tracks CPU RK4 rather than collapsing to Euler."""
    pos, vel, masses, ids = _make_small_cluster(n=6, seed=3)
    dt = 1e-2
    n_steps = 5
    eps2 = 1e-4

    cpu_rk4_p, _ = _cpu_run_monolithic("rk4", pos, vel, masses, ids, dt, n_steps, eps2)
    gpu_rk4_p, _ = _gpu_run_monolithic("rk4", pos, vel, masses, ids, dt, n_steps, eps2)
    cpu_se_p, _ = _cpu_run_monolithic("symplectic_euler", pos, vel, masses, ids, dt, n_steps, eps2)

    rk4_diff = np.max(np.abs(gpu_rk4_p - cpu_rk4_p))
    euler_diff = np.max(np.abs(gpu_rk4_p - cpu_se_p))
    assert rk4_diff < 1e-8, f"GPU RK4 diverges from CPU RK4: {rk4_diff:.3e}"
    assert rk4_diff * 100 < euler_diff, (
        f"GPU RK4 too close to symplectic Euler (rk4_diff={rk4_diff:.3e}, "
        f"euler_diff={euler_diff:.3e}) — likely silent fallback."
    )


def test_velocity_verlet_state_resets_on_update_bodies_from_arrays():
    """After update_bodies_from_arrays(), GPU VV recomputes the initial accelerations
    (vv_initialized is reset)."""
    pos_a, vel_a, masses, ids = _make_two_body()
    pos_b, vel_b, _, _ = _make_two_body()
    pos_b[1] += np.array([0.5, 0.3, -0.1])

    dt = 1e-3
    eps2 = 1e-9

    bodies_a = _bodies_dict(pos_a, vel_a, masses, ids)
    bodies_b = _bodies_dict(pos_b, vel_b, masses, ids)

    reused = GPUNBodyEngine(
        bodies=bodies_a, time_step=dt, integration_method="velocity_verlet",
        softening_squared=eps2, gravitational_constant=1.0,
    )
    try:
        for _ in range(5):
            reused.simulate_single_step()
        reused.update_bodies_from_arrays(pos_b, vel_b, masses, ids)
        reused.simulate_single_step()
        reused_state = reused.get_state_arrays()
    finally:
        reused.cleanup()

    fresh = GPUNBodyEngine(
        bodies=bodies_b, time_step=dt, integration_method="velocity_verlet",
        softening_squared=eps2, gravitational_constant=1.0,
    )
    try:
        fresh.simulate_single_step()
        fresh_state = fresh.get_state_arrays()
    finally:
        fresh.cleanup()

    for r, f in zip(reused_state, fresh_state):
        np.testing.assert_allclose(r, f, rtol=0, atol=1e-12, err_msg="VV step diverged after update_bodies_from_arrays()")


# =============================================================================
# Backend-level parity (drives GPUBackend, not the raw engine)
# =============================================================================
# The engine tests above all construct GPUNBodyEngine directly with
# gravitational_constant=1.0. That verifies engine<->CPU parity but NEVER
# exercises GPUBackend, which is the layer the monolithic/distributed workers
# actually call. A bug where the backend forgot to forward G to the engine
# (engine then silently used its SI default 6.67e-11 instead of the normalized
# G_CONST=1) produced physically wrong GPU jobs while every engine test still
# passed. These tests run through the real backend API — which takes NO G
# argument — so the only correct value is the one the backend wires in itself.

def _backend_run_monolithic(method, pos, vel, masses, ids, dt, n_steps, eps2):
    backend = GPUBackend()
    p, v = pos.copy(), vel.copy()
    prev_accel = None
    try:
        for _ in range(n_steps):
            p, v, prev_accel = backend.compute_step(
                positions=p, velocities=v, masses=masses, ids=ids,
                time_step=dt, algorithm='direct', integration_method=method,
                softening_sq=eps2, previous_accelerations=prev_accel,
            )
        return p, v
    finally:
        cleanup = getattr(backend, 'cleanup', None)
        if cleanup is not None:
            cleanup()


@pytest.mark.parametrize("method", INTEGRATION_METHODS)
def test_backend_monolithic_parity_uses_normalized_G(method):
    """GPUBackend.compute_step must match the CPU physics reference, which
    uses G_CONST: the backend takes no gravitational_constant, so if it did
    not forward G_CONST the GPU sim would run ~1e10x too weak.
    """
    pos, vel, masses, ids = _make_small_cluster(n=8, seed=7)
    dt = 5e-4
    n_steps = 30
    eps2 = 1e-6

    cpu_p, cpu_v = _cpu_run_monolithic(method, pos, vel, masses, ids, dt, n_steps, eps2)
    gpu_p, gpu_v = _backend_run_monolithic(method, pos, vel, masses, ids, dt, n_steps, eps2)

    np.testing.assert_allclose(gpu_p, cpu_p, atol=1e-9, rtol=1e-8,
                               err_msg=f"{method}: backend position mismatch (check G wiring)")
    np.testing.assert_allclose(gpu_v, cpu_v, atol=1e-9, rtol=1e-8,
                               err_msg=f"{method}: backend velocity mismatch (check G wiring)")


def test_backend_engine_G_equals_G_CONST():
    """Sharp, tolerance-free guard: after the backend builds its engine, the
    engine's G must equal the CPU G_CONST — not the engine's SI default.
    """

    pos, vel, masses, ids = _make_small_cluster(n=4, seed=1)
    backend = GPUBackend()
    try:
        backend.compute_step(
            positions=pos, velocities=vel, masses=masses, ids=ids,
            time_step=1e-3, algorithm='direct', integration_method='symplectic_euler',
            softening_sq=1e-6,
        )
        assert backend._engine.G == G_CONST, (
            f"backend engine G={backend._engine.G!r} != G_CONST={G_CONST!r}, "
            "backend dropped gravitational_constant"
        )
    finally:
        cleanup = getattr(backend, 'cleanup', None)
        if cleanup is not None:
            cleanup()


@pytest.mark.parametrize("variant,base", [("gvv_accel", "velocity_verlet"), ("grk4_linear", "rk4"),
                                          ("gfr_accel", "forest_ruth")])
def test_backend_monolithic_accepts_distributed_method_names(variant, base):
    """The API accepts every IntegrationMethod for a monolithic GPU job. The
    engine knows only the base integrators, so the backend resolves a g-named
    variant to its base method (on the whole system the predictor is moot)
    instead of failing the job in the engine constructor."""
    pos, vel, masses, ids = _make_small_cluster(n=8, seed=3)
    want = _backend_run_monolithic(base, pos, vel, masses, ids, 5e-4, 5, 1e-6)
    got = _backend_run_monolithic(variant, pos, vel, masses, ids, 5e-4, 5, 1e-6)
    assert np.array_equal(got[0], want[0]) and np.array_equal(got[1], want[1])


def test_package_step_reads_back_the_package_rows():
    """The package step reads back only its K rows: they must equal the same
    rows of a full read-back."""
    from physics_gpu import GPUNBodyEngine
    pos, vel, masses, ids = _make_small_cluster(n=40, seed=5)
    eng = GPUNBodyEngine(bodies=_bodies_dict(pos, vel, masses, ids), time_step=1e-3, integration_method="rk4",
                         gravitational_constant=1.0, softening_squared=1e-4)
    try:
        p, v, _ = eng.simulate_package_step(10, 15, predictor_order=1)
        all_p, all_v = eng.get_state_arrays()
    finally:
        eng.cleanup()
    assert np.array_equal(p, all_p[10:25]) and np.array_equal(v, all_v[10:25])


@pytest.mark.parametrize("method", ["rk4", "forest_ruth", "velocity_verlet"])
def test_package_scratch_buffers_reused_across_sizes(method):
    """The package step's scratch buffers are kept between calls and regrown
    for a larger package: a reused engine (packages of 15, then 5, then 20
    bodies) must give what a fresh engine gives for each."""
    from physics_gpu import GPUNBodyEngine
    pos, vel, masses, ids = _make_small_cluster(n=40, seed=6)
    bodies = [dict(n=i, idBody=int(ids[i]), m=float(masses[i]), x=pos[i, 0], y=pos[i, 1], z=pos[i, 2],
                   vx=vel[i, 0], vy=vel[i, 1], vz=vel[i, 2]) for i in range(len(ids))]

    def engine():
        return GPUNBodyEngine(bodies=bodies, time_step=1e-3, integration_method=method,
                              gravitational_constant=1.0, softening_squared=1e-4)
    reused = engine()
    try:
        for start, k in ((10, 15), (0, 5), (20, 20)):
            reused.update_bodies_from_arrays(pos, vel, masses, ids)
            got = reused.simulate_package_step(start, k, predictor_order=1)
            fresh = engine()
            try:
                want = fresh.simulate_package_step(start, k, predictor_order=1)
            finally:
                fresh.cleanup()
            for a, b in zip(got, want):
                assert np.array_equal(a, b)
    finally:
        reused.cleanup()


@pytest.mark.parametrize("method", INTEGRATION_METHODS)
def test_adaptive_resident_stepping_matches_the_cpu(method):
    """The monolithic GPU worker's adaptive path: resident steps whose dt comes
    from the accelerations each step reads back take the same steps as the CPU
    and end in the same state, to round-off."""
    rng = np.random.default_rng(3)
    n, steps, eps2 = 64, 200, 0.01
    pos0, vel0 = rng.standard_normal((n, 3)), 0.3 * rng.standard_normal((n, 3))
    masses, ids = rng.uniform(0.5, 1.5, n), np.arange(1, n + 1, dtype=np.int32)
    kw = dict(eta=0.02, softening=eps2 ** 0.5, dt_min=1e-6, dt_max=5e-3)

    cpu = CPUBackend()
    pos, vel, acc, dt, cpu_dts = pos0.copy(), vel0.copy(), None, 5e-3, []
    for _ in range(steps):
        if acc is not None:
            dt = physics.estimate_adaptive_dt(acc, dt, **kw)
        cpu_dts.append(dt)
        pos, vel, acc = cpu.compute_step(
            pos, vel, masses, ids, dt, algorithm="direct", integration_method=method, softening_sq=eps2,
            previous_accelerations=acc if method == "velocity_verlet" else None)

    gpu = GPUBackend()
    try:
        gpu.load_resident_state(pos0, vel0, masses, ids, time_step=5e-3, integration_method=method,
                                softening_sq=eps2)
        acc, dt, gpu_dts = None, 5e-3, []
        for _ in range(steps):
            if acc is not None:
                dt = physics.estimate_adaptive_dt(acc, dt, **kw)
            gpu_dts.append(dt)
            acc = gpu.step_resident(dt, need_accelerations=True)
        gpu_pos, gpu_vel, _ = gpu.fetch_resident_state()
    finally:
        gpu.cleanup()

    assert len(set(cpu_dts)) > 1, "the step never changed: not an adaptive run"
    np.testing.assert_allclose(gpu_dts, cpu_dts, rtol=1e-12)
    np.testing.assert_allclose(gpu_pos, pos, rtol=0, atol=1e-12)
    np.testing.assert_allclose(gpu_vel, vel, rtol=0, atol=1e-12)
