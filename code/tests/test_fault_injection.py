# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The V2 worker's fault-injection hook (NBODY_FAULT_INJECT)."""
from __future__ import annotations

import os

import pytest

from helpers import requires_redis
from workers.distributed_worker_v2 import fault_point


pytestmark = requires_redis


class _Exit(Exception):
    pass


@pytest.fixture
def exits(monkeypatch):
    calls = []

    def fake_exit(code):
        calls.append(code)
        raise _Exit(code)

    monkeypatch.setattr(os, "_exit", fake_exit)   # fault_point imports os locally
    return calls


def test_no_injection_without_the_variable(svc, exits, monkeypatch):
    monkeypatch.delenv("NBODY_FAULT_INJECT", raising=False)
    fault_point("before_commit", 1, 5, "w", svc)
    assert exits == []


def test_injects_once_at_the_named_point_and_epoch(svc, exits, monkeypatch):
    monkeypatch.setenv("NBODY_FAULT_INJECT", "after_commit@5, mid_task@7")
    fault_point("after_commit", 1, 4, "w", svc)        # other epoch
    fault_point("before_commit", 1, 5, "w", svc)       # other point
    assert exits == []
    with pytest.raises(_Exit):
        fault_point("after_commit", 1, 5, "w1", svc)
    assert exits == [137]
    fault_point("after_commit", 1, 5, "w2", svc)       # replacement pod: no second death
    assert exits == [137]
    assert svc.client.get("v2:fault:1:after_commit:5").startswith(b"w1|")
    with pytest.raises(_Exit):
        fault_point("mid_task", 1, 7, "w3", svc)


def test_injection_can_be_scoped_to_one_job(svc, exits, monkeypatch):
    monkeypatch.setenv("NBODY_FAULT_INJECT", "before_commit@3@42")
    fault_point("before_commit", 7, 3, "w", svc)       # another job: untouched
    assert exits == []
    with pytest.raises(_Exit):
        fault_point("before_commit", 42, 3, "w", svc)
