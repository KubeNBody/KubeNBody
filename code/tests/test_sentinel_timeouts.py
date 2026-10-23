# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Sentinel connections carry timeouts.

redis-py configures the connections to the Sentinels from sentinel_kwargs
alone. With only the password there, a pooled connection to a Sentinel that
vanished with its pod (no RST) blocked discover_master() until the kernel gave
up on TCP (~15 min), stalling the pipeline for that long after the Redis
primary was killed. These tests use a fake Sentinel that accepts connections and never
answers, which blocks forever without the timeouts.
"""
from __future__ import annotations

import socket
import threading
import time
import types

import pytest
import redis

import v2.redis_service as R


@pytest.fixture
def silent_sentinel():
    """A TCP server that accepts connections and never replies."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    held, stop = [], threading.Event()

    def accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                held.append(conn)
            except OSError:
                continue
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield srv.getsockname()
    stop.set()
    t.join(2)
    for c in held:
        c.close()
    srv.close()


def test_a_silent_sentinel_fails_fast_instead_of_hanging(monkeypatch, silent_sentinel):
    host, port = silent_sentinel
    monkeypatch.setattr(type(R.cfg), "get_sentinel_hosts", lambda self: [(host, port)])
    monkeypatch.setattr(R, "SENTINEL_SOCKET_TIMEOUT_S", 1)
    svc = R.RedisService.__new__(R.RedisService)
    svc.sentinel = None
    t0 = time.monotonic()
    with pytest.raises((redis.exceptions.ConnectionError, redis.exceptions.TimeoutError,
                        redis.sentinel.MasterNotFoundError)):
        svc._connect_sentinel()
    assert time.monotonic() - t0 < 10          # was: blocked until TCP gave up


def test_sentinel_and_master_connections_have_timeouts_and_keepalive(monkeypatch):
    seen = {}

    class FakeSentinel:
        def __init__(self, hosts, **kwargs):
            seen["sentinel"] = kwargs

        def master_for(self, name, **kwargs):
            seen["master"] = kwargs
            return types.SimpleNamespace(ping=lambda: True, execute_command=lambda *a, **k: None,
                                         pipeline=lambda *a, **k: None)

        def slave_for(self, name, **kwargs):
            seen["replica"] = kwargs
            return types.SimpleNamespace(execute_command=lambda *a, **k: None,
                                         pipeline=lambda *a, **k: None)

        def discover_master(self, name):
            return ("10.0.0.1", 6379)
    monkeypatch.setattr(R, "Sentinel", FakeSentinel)
    monkeypatch.setattr(type(R.cfg), "get_sentinel_hosts", lambda self: [("s", 26379)])
    svc = R.RedisService.__new__(R.RedisService)
    svc.sentinel = None
    svc._use_sentinel = True
    svc._connect_sentinel()
    sk = seen["sentinel"]["sentinel_kwargs"]
    assert sk["socket_timeout"] and sk["socket_connect_timeout"] and sk["socket_keepalive"]
    m = seen["master"]
    assert m["socket_timeout"] and m["socket_connect_timeout"] and m["socket_keepalive"]
    svc._replica_down_until = 0.0
    svc.replica_client = None
    monkeypatch.setattr(R.cfg, "replica_reads", True)
    svc._replica()
    r = seen.get("replica")
    assert r and r["socket_connect_timeout"] and r["socket_keepalive"]
