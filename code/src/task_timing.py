# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Per-task and per-merge phase timing for the V2 pipeline.

Enabled with NBODY_TIMING_DETAIL=1. Each computed task and each merge appends
one JSON record to the Redis list v2:timings:{job_id} (outside the job's key
namespace, so it survives job cleanup, 7-day TTL): read them with
LRANGE v2:timings:{job_id}.

A task record holds the phase durations of one package step (fetch and decode
of the epoch state and accelerations, compute, encode, register, load_state_total
covers fetch + decode), the bytes read and written, and the number of Redis
round trips from the fence pre-check to the registration (the claim, XREADGROUP
or XAUTOCLAIM, and the XACK happen outside the record: add 2 per task, 3 for a
reclaimed one). A merge record holds the phases of the epoch transition.
With the flag off (the default) only the timers run, so the measured
durations carry no Redis overhead.

NBODY_SNAPSHOT_DIAGNOSTICS=1 additionally records total energy, momentum and
angular momentum of the merged state at every snapshot epoch (and of the
initial state) in v2:diag:{job_id}. That costs one O(N^2) pass per snapshot on
the merging worker. Its duration is reported in the merge record.
"""
import os
import time
from contextlib import contextmanager
from typing import Dict, Optional

import numpy as np

from diagnostics import total_angular_momentum, total_energy, total_momentum
from v2.binary_format import binary_to_numpy


def _flag(name: str) -> bool:
    return os.environ.get(name, '0').strip().lower() in ('1', 'true', 'yes', 'on')


TIMING_ENABLED = _flag('NBODY_TIMING_DETAIL')
DIAGNOSTICS_ENABLED = _flag('NBODY_SNAPSHOT_DIAGNOSTICS')


class PhaseTimer:
    """Accumulates named phase durations (seconds) and counters for one unit
    of work. Cheap enough to run unconditionally."""

    def __init__(self, kind: str):
        self.kind = kind
        self.wall_start = time.time()
        self._t0 = time.perf_counter()
        self.phases: Dict[str, float] = {}
        self.counters: Dict[str, int] = {}

    @contextmanager
    def phase(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.phases[name] = self.phases.get(name, 0.0) + time.perf_counter() - t0

    def add(self, name: str, value: int) -> None:
        self.counters[name] = self.counters.get(name, 0) + int(value)

    def elapsed(self) -> float:
        return time.perf_counter() - self._t0

    def record(self, **fields) -> dict:
        total = self.elapsed()
        rec = {'kind': self.kind, 't_start': self.wall_start, 'total_s': total}
        rec.update(fields)
        rec.update({f'{k}_s': v for k, v in self.phases.items()})
        # the part of total_s no phase covers (phases do not nest), so a gap
        # between phases shows up in the record instead of being inferred
        rec['untimed_s'] = total - sum(self.phases.values())
        rec.update(self.counters)
        return rec


def energy_diagnostics(merged: bytes, softening_sq: float) -> Optional[dict]:
    """Energy, momentum and angular momentum of a binary body state."""
    pos, vel, masses, _ids = binary_to_numpy(merged, validate=False)
    ke, pe = total_energy(pos, vel, masses, softening_sq, 1.0)
    p = total_momentum(vel, masses)
    lvec = total_angular_momentum(pos, vel, masses)
    return {
        'kinetic': float(ke), 'potential': float(pe), 'energy': float(ke + pe),
        'momentum': [float(x) for x in np.asarray(p)],
        'angular_momentum': [float(x) for x in np.asarray(lvec)],
    }
