# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: initial conditions for the physics tests, and a real
redis-server with the V2 pipeline around it for the coordination tests."""
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types


# Numba's on-disk cache (cache=True kernels) goes to a fresh directory per test
# session unless NUMBA_CACHE_DIR is set: kernels cached by an earlier run (or
# another Python/Numba build) in src/__pycache__ segfaulted when reloaded.
# Set before any test imports numba. Removed again when the session ends.
if "NUMBA_CACHE_DIR" not in os.environ:
    import atexit
    import shutil
    os.environ["NUMBA_CACHE_DIR"] = tempfile.mkdtemp(prefix="nbody-numba-")
    atexit.register(shutil.rmtree, os.environ["NUMBA_CACHE_DIR"], ignore_errors=True)

from models_stub import models_stub_module  # noqa: E402


# Before anything imports src/: no test talks to PostgreSQL
sys.modules.setdefault("models", models_stub_module())

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from helpers import REDIS_SERVER, create_job, drive, redis, use_models, v2_worker  # noqa: E402
from v2.redis_service import RedisService, _count_round_trips  # noqa: E402


# =============================================================================
# Redis and the V2 pipeline
# =============================================================================

@pytest.fixture(scope="module")
def redis_port():
    """A throwaway redis-server on a free port (the coordination logic lives in
    Lua scripts, so it is tested against the real server)."""
    if redis is None or REDIS_SERVER is None:
        pytest.skip("redis-server not installed")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [REDIS_SERVER, "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    client = redis.Redis(port=port)
    for _ in range(100):
        try:
            client.ping()
            break
        except redis.ConnectionError:
            time.sleep(0.05)
    yield port
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def svc(redis_port):
    """A RedisService on the module's redis-server, flushed."""
    s = RedisService.__new__(RedisService)
    s._use_sentinel = False
    s._lock = threading.Lock()
    s._reclaim_cursors, s._scripts = {}, {}
    s.client = redis.Redis(port=redis_port, decode_responses=False)
    _count_round_trips(s.client)       # as _connect does in production
    s.client.flushdb()
    return s


@pytest.fixture
def no_pg(monkeypatch):
    """Stand-in for the PostgreSQL models module (progress sync, fail_job)."""
    class _Q:
        def where(self, *a, **k):
            return self

        def execute(self):
            return 0

    class _Field:
        def __eq__(self, other):
            return _Field()

        def __and__(self, other):
            return _Field()

        def not_in(self, values):
            return _Field()

        def is_null(self, flag=True):
            return _Field()

    class Job:
        job_id = _Field()
        status = _Field()
        completed_at = _Field()
        started_at = _Field()

        @staticmethod
        def update(**kw):
            return _Q()

        @staticmethod
        def get(*a, **k):
            return object()

        @staticmethod
        def get_or_none(*a, **k):
            return None

    use_models(monkeypatch, types.SimpleNamespace(Job=Job))


@pytest.fixture
def reference_state(svc, no_pg):
    """Final state of an uninterrupted rk4 run of the harness job."""
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    state = finals["state"]
    svc.client.flushdb()
    return state


# =============================================================================
# Two-Body Kepler Orbit ICs
# =============================================================================

@pytest.fixture
def kepler_circular():
    """Two-body circular orbit (e=0). Period T = 2*pi for G=1, M=1, a=1."""
    m1, m2 = 1.0, 1e-6
    r = 1.0
    v_circ = np.sqrt((m1 + m2) / r)

    positions = np.array([[0.0, 0.0, 0.0], [r, 0.0, 0.0]], dtype=np.float64)
    velocities = np.array([[0.0, 0.0, 0.0], [0.0, v_circ, 0.0]], dtype=np.float64)
    masses = np.array([m1, m2], dtype=np.float64)
    ids = np.array([0, 1], dtype=np.int32)

    # Move to center-of-mass frame
    total_mass = m1 + m2
    com_pos = (masses[:, None] * positions).sum(axis=0) / total_mass
    com_vel = (masses[:, None] * velocities).sum(axis=0) / total_mass
    positions -= com_pos
    velocities -= com_vel

    return positions, velocities, masses, ids


@pytest.fixture
def kepler_eccentric():
    """Two-body eccentric orbit (e=0.5). Started at periapse."""
    m1, m2 = 1.0, 1e-6
    e = 0.5
    a = 1.0
    r_peri = a * (1 - e)
    v_peri = np.sqrt((m1 + m2) * (1 + e) / (a * (1 - e)))

    positions = np.array([[0.0, 0.0, 0.0], [r_peri, 0.0, 0.0]], dtype=np.float64)
    velocities = np.array([[0.0, 0.0, 0.0], [0.0, v_peri, 0.0]], dtype=np.float64)
    masses = np.array([m1, m2], dtype=np.float64)
    ids = np.array([0, 1], dtype=np.int32)

    total_mass = m1 + m2
    com_pos = (masses[:, None] * positions).sum(axis=0) / total_mass
    com_vel = (masses[:, None] * velocities).sum(axis=0) / total_mass
    positions -= com_pos
    velocities -= com_vel

    return positions, velocities, masses, ids


@pytest.fixture
def kepler_highly_eccentric():
    """Two-body highly eccentric orbit (e=0.9). Stress test for integrators."""
    m1, m2 = 1.0, 1e-6
    e = 0.9
    a = 1.0
    r_peri = a * (1 - e)
    v_peri = np.sqrt((m1 + m2) * (1 + e) / (a * (1 - e)))

    positions = np.array([[0.0, 0.0, 0.0], [r_peri, 0.0, 0.0]], dtype=np.float64)
    velocities = np.array([[0.0, 0.0, 0.0], [0.0, v_peri, 0.0]], dtype=np.float64)
    masses = np.array([m1, m2], dtype=np.float64)
    ids = np.array([0, 1], dtype=np.int32)

    total_mass = m1 + m2
    com_pos = (masses[:, None] * positions).sum(axis=0) / total_mass
    com_vel = (masses[:, None] * velocities).sum(axis=0) / total_mass
    positions -= com_pos
    velocities -= com_vel

    return positions, velocities, masses, ids


# =============================================================================
# Multi-Body ICs
# =============================================================================

@pytest.fixture
def plummer_sphere_small():
    """Small Plummer sphere (N=50) in approximate virial equilibrium."""
    np.random.seed(42)
    n = 50
    total_mass = 1.0
    scale_length = 1.0
    m_per_body = total_mass / n

    positions = np.zeros((n, 3), dtype=np.float64)
    velocities = np.zeros((n, 3), dtype=np.float64)
    masses = np.full(n, m_per_body, dtype=np.float64)
    ids = np.arange(n, dtype=np.int32)

    for i in range(n):
        u = np.random.uniform(0.001, 0.999)
        r = scale_length / np.sqrt(u ** (-2.0 / 3.0) - 1.0)

        cos_theta = 2.0 * np.random.uniform() - 1.0
        sin_theta = np.sqrt(1.0 - cos_theta ** 2)
        phi = 2.0 * np.pi * np.random.uniform()

        positions[i] = [r * sin_theta * np.cos(phi),
                        r * sin_theta * np.sin(phi),
                        r * cos_theta]

        v_escape = np.sqrt(2.0 * total_mass / np.sqrt(scale_length ** 2 + r ** 2))
        while True:
            q = np.random.uniform(0.0, 1.0)
            g = np.random.uniform(0.0, 0.1)
            if g < q * q * (1.0 - q * q) ** 3.5:
                break
        speed = q * v_escape

        cos_theta_v = 2.0 * np.random.uniform() - 1.0
        sin_theta_v = np.sqrt(1.0 - cos_theta_v ** 2)
        phi_v = 2.0 * np.pi * np.random.uniform()

        velocities[i] = [speed * sin_theta_v * np.cos(phi_v),
                         speed * sin_theta_v * np.sin(phi_v),
                         speed * cos_theta_v]

    # Zero out center-of-mass position and velocity
    com_pos = (masses[:, None] * positions).sum(axis=0) / total_mass
    com_vel = (masses[:, None] * velocities).sum(axis=0) / total_mass
    positions -= com_pos
    velocities -= com_vel

    return positions, velocities, masses, ids


