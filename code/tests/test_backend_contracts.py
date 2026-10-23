# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Compute-backend contracts:
1. softening_sq defaults: the CPU/GPU backend layer must default to
   constants.SOFTENING_SQ (not a placeholder like 1.0) so that callers
   who forget to pass softening get the same physics as direct calls to
   physics.compute_accelerations.
2. Barnes-Hut + GPU rejection: the GPU backend must raise ComputeError
   instead of silently downgrading to direct (a silent algorithm change).

These tests deliberately do NOT depend on PyCUDA or a live GPU — we can
inspect ComputeError behavior just by constructing the backend and
calling its public methods. They also avoid importing the workers
package via `workers.__init__`, since that triggers DB connections.
"""
from __future__ import annotations

import inspect
import os
import subprocess
import sys

import numpy as np
import pytest

import workers.backends.gpu_backend as gpu_backend_module
from constants import SOFTENING_SQ
from workers.backends.cpu_backend import CPUBackend
from workers.base.compute_backend import ComputeBackend, ComputeError


# ---------------------------------------------------------------------------
# Softening default alignment
# ---------------------------------------------------------------------------

def _softening_default(callable_obj):
    """Return the default value of the `softening_sq` parameter, or None."""
    sig = inspect.signature(callable_obj)
    param = sig.parameters.get('softening_sq')
    return None if param is None or param.default is inspect.Parameter.empty else param.default


def test_compute_backend_abc_softening_default_uses_constant():
    assert _softening_default(ComputeBackend.compute_step) == SOFTENING_SQ
    assert _softening_default(ComputeBackend.compute_package_step) == SOFTENING_SQ


def test_cpu_backend_softening_default_uses_constant():
    assert _softening_default(CPUBackend.compute_step) == SOFTENING_SQ
    assert _softening_default(CPUBackend.compute_package_step) == SOFTENING_SQ


def test_gpu_backend_softening_default_uses_constant():
    # Import lazily so that PyCUDA-less dev boxes still collect this file.
    try:
        from workers.backends.gpu_backend import GPUBackend
    except ImportError:  # PyCUDA missing — GPU backend itself can still import
        pytest.skip("GPUBackend module not importable (PyCUDA missing)")
    assert _softening_default(GPUBackend.compute_step) == SOFTENING_SQ
    assert _softening_default(GPUBackend.compute_package_step) == SOFTENING_SQ


def test_softening_default_not_one_point_zero():
    """Specific guard: previous default was 1.0 which silently disabled gravity
    for close pairs in normalized units. Ensure no signature still has it.
    """
    for fn in (ComputeBackend.compute_step, ComputeBackend.compute_package_step,
               CPUBackend.compute_step, CPUBackend.compute_package_step):
        assert _softening_default(fn) != 1.0, (
            f"{fn.__qualname__} still defaults softening_sq=1.0 (regression)"
        )


# ---------------------------------------------------------------------------
# Barnes-Hut + GPU rejection
# ---------------------------------------------------------------------------

@pytest.fixture
def gpu_backend(monkeypatch):
    """A GPUBackend that reports a GPU: the Barnes-Hut check comes before any
    CUDA call, so it runs without a device."""
    monkeypatch.setattr(gpu_backend_module, "GPU_AVAILABLE", True)
    return gpu_backend_module.GPUBackend()


def _two_bodies():
    return (np.zeros((2, 3)), np.zeros((2, 3)), np.ones(2), np.array([0, 1], dtype=np.int32))


def test_gpu_backend_rejects_barnes_hut_monolithic(gpu_backend):
    """Barnes-Hut on the GPU backend is a non-recoverable ComputeError, not a
    silent downgrade to direct summation."""
    pos, vel, masses, ids = _two_bodies()
    with pytest.raises(ComputeError, match="(?i)barnes-hut") as excinfo:
        gpu_backend.compute_step(positions=pos, velocities=vel, masses=masses, ids=ids,
                                 time_step=0.001, algorithm='barnes_hut')
    assert excinfo.value.recoverable is False


def test_gpu_backend_rejects_barnes_hut_package(gpu_backend):
    pos, vel, masses, ids = _two_bodies()
    with pytest.raises(ComputeError, match="(?i)barnes-hut") as excinfo:
        gpu_backend.compute_package_step(
            k_positions=pos, k_velocities=vel, k_masses=masses, k_ids=ids,
            all_positions=pos, all_velocities=vel, all_masses=masses, all_ids=ids,
            time_step=0.001, algorithm='barnes_hut',
        )
    assert excinfo.value.recoverable is False


# ---------------------------------------------------------------------------
# CPU backend still accepts Barnes-Hut (sanity)
# ---------------------------------------------------------------------------

def test_cpu_backend_accepts_barnes_hut():
    """The rejection is GPU-only. CPU must still happily run Barnes-Hut."""
    backend = CPUBackend()
    backend.warmup()

    pos = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float64)
    vel = np.zeros((2, 3), dtype=np.float64)
    masses = np.ones(2, dtype=np.float64)
    ids = np.array([0, 1], dtype=np.int32)

    new_pos, new_vel, new_accel = backend.compute_step(
        positions=pos, velocities=vel, masses=masses, ids=ids,
        time_step=1e-3, algorithm='barnes_hut',
    )
    assert new_pos.shape == pos.shape
    assert new_vel.shape == vel.shape
    assert new_accel.shape == pos.shape


# ---------------------------------------------------------------------------
# Package-step return signature
# ---------------------------------------------------------------------------

def test_compute_package_step_returns_triple():
    """The CPU backend's compute_package_step returns (pos, vel, accel), as
    distributed_worker.py expects."""
    backend = CPUBackend()
    backend.warmup()

    pos = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float64)
    vel = np.zeros((2, 3), dtype=np.float64)
    masses = np.ones(2, dtype=np.float64)
    ids = np.array([0, 1], dtype=np.int32)

    result = backend.compute_package_step(
        k_positions=pos, k_velocities=vel, k_masses=masses, k_ids=ids,
        all_positions=pos, all_velocities=vel, all_masses=masses, all_ids=ids,
        time_step=1e-3,
    )
    assert len(result) == 3, "Expected (pos, vel, accel) 3-tuple"
    new_pos, new_vel, new_accel = result
    assert new_pos.shape == pos.shape
    assert new_vel.shape == vel.shape
    assert new_accel.shape == pos.shape
    # Accelerations on two equal-mass bodies separated by 1.0 in natural
    # units should be non-zero (attractive force along x).
    assert np.linalg.norm(new_accel) > 0


# ---------------------------------------------------------------------------
# workers/__init__.py lazy loading (importing a backend opens no DB connection)
# ---------------------------------------------------------------------------

def test_importing_workers_package_does_not_open_db_connection():
    """`import workers` and `from workers.backends.cpu_backend import CPUBackend`
    must NOT eagerly load monolithic_worker / models (and the S3 clients and
    pandas they pull in).
    """
    # In a fresh interpreter: earlier tests (in any order) may already have
    # imported the worker modules.
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    code = ("import sys, workers; from workers.backends.cpu_backend import CPUBackend; "
            "print(' '.join(m for m in ('workers.monolithic_worker', 'workers.distributed_worker', 'models') "
            "if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
                         env={**os.environ, "PYTHONPATH": src}, cwd=src)
    assert out.returncode == 0, out.stderr[-2000:]
    loaded = out.stdout.split()
    assert 'workers.monolithic_worker' not in loaded, (
        "workers/__init__.py eagerly loaded monolithic_worker — lazy loading regressed"
    )
    assert 'workers.distributed_worker' not in loaded, (
        "workers/__init__.py eagerly loaded distributed_worker — lazy loading regressed"
    )


def test_gpu_engine_cleanup_frees_every_device_buffer_once():
    """cleanup() left the six RK4 buffers allocated, leaked on every engine
    rebuild in a long-lived worker."""
    from physics_gpu import GPUNBodyEngine
    freed = []

    class Buf:
        def __init__(self, name):
            self.name = name

        def free(self):
            freed.append(self.name)
    engine = GPUNBodyEngine.__new__(GPUNBodyEngine)
    for name in GPUNBodyEngine._DEVICE_BUFFERS:
        setattr(engine, name, Buf(name))
    engine.cleanup()
    engine.cleanup()                                   # e.g. again from __del__
    assert sorted(freed) == sorted(GPUNBodyEngine._DEVICE_BUFFERS)
