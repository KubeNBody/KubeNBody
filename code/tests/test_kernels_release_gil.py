# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The numba kernels release the GIL (nogil=True).

While a kernel holding the GIL ran, no other Python thread could run: the
task heartbeat, the monolithic lease renewer and the health probes all
stalled for the length of the call, which kept the recovery time-outs long.
"""
from __future__ import annotations

import threading
import time

import numpy as np
from numba.core.registry import CPUDispatcher

import barnes_hut
import diagnostics
import physics


def test_every_kernel_is_compiled_without_the_gil():
    held = [f"{mod.__name__}.{name}"
            for mod in (physics, barnes_hut, diagnostics)
            for name, f in vars(mod).items()
            if isinstance(f, CPUDispatcher) and f.py_func.__module__ == mod.__name__
            and not f.targetoptions.get('nogil')]
    assert held == []


def test_another_thread_runs_while_a_kernel_computes():
    """A Python thread keeps running during one long kernel call: its
    longest pause is a small part of the call, not the whole call."""
    rng = np.random.default_rng(0)
    n = 16000
    pos, vel = rng.normal(size=(n, 3)), np.zeros((n, 3))
    masses, ids = np.ones(n), np.arange(n, dtype=np.int64)
    physics.step_rk4(pos[:10], vel[:10], masses[:10], ids[:10], 1e-3)     # compile first

    stamps, stop = [], threading.Event()

    def ticker():
        while not stop.is_set():
            stamps.append(time.perf_counter())
            time.sleep(0.001)

    t = threading.Thread(target=ticker, daemon=True)
    t.start()
    time.sleep(0.02)
    call_start = time.perf_counter()
    physics.step_rk4(pos, vel, masses, ids, 1e-3)       # four force evaluations
    call_end = time.perf_counter()
    stop.set()
    t.join()
    during = [s for s in stamps if call_start <= s <= call_end]
    edges = [call_start] + during + [call_end]
    longest_pause = max(b - a for a, b in zip(edges, edges[1:]))
    duration = call_end - call_start
    # held, the thread would not run at all during the call (one pause = the call)
    assert len(during) >= 5 and longest_pause < 0.5 * duration, (len(during), longest_pause, duration)
