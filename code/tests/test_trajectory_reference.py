# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Every integrator, force algorithm and engine still steps the reference
cluster to exactly the recorded state (trajectories.py, which also says how to
record a machine's trajectories). A refactor of the steppers must leave these
bitwise unchanged."""
import numpy as np
import pytest
import trajectories as T

from workers.backends.gpu_backend import GPU_AVAILABLE


def _compare(key, cases):
    if not T.os.path.exists(T.path_for(key)):
        pytest.skip(f"no recorded trajectories for {key} (record them with python -m trajectories)")
    recorded = T.load(key)
    assert sorted(cases) == sorted(recorded), "the recorded cases differ from the current ones"
    changed = [name for name in sorted(cases)
               if not all(np.array_equal(a, b) for a, b in zip(cases[name], recorded[name]))]
    assert not changed, f"{len(changed)} of {len(cases)} trajectories changed: {', '.join(changed)}"


def test_cpu_trajectories_are_unchanged():
    _compare(T.cpu_key(), T.cpu_cases())


@pytest.mark.skipif(not GPU_AVAILABLE, reason="needs a CUDA device")
def test_gpu_trajectories_are_unchanged():
    _compare(T.gpu_key(), T.gpu_cases())
