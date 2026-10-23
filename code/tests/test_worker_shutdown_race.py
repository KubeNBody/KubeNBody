# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""SIGTERM must never abandon a claimed task.

A signal handler that exits when no task is marked as in progress loses a task
whose message arrives during claim_task (or between claim_task returning and
_processing_task being set): it stays in the dead consumer's pending list
until the stale-task reclaim, 300 s by default, which stalled elasticity runs
at each scale-down of the worker pool. The handler only sets a flag.

Likewise a worker whose GPU broke (a CUDA fault) exits only after handing its
task back.
"""
from __future__ import annotations

import signal
import types

import pytest

import workers.distributed_worker as DW
from workers.distributed_worker import CLAIM_BLOCK_MS, DistributedWorker


class _Messages:
    """Fake backend: the first claim delivers a task while SIGTERM arrives."""

    name = "fake"

    def __init__(self, worker, signal_during_claim: bool):
        self.worker, self.signal_during_claim = worker, signal_during_claim
        self.claims, self.acked, self.nacked = 0, [], []

    def claim_stale_task(self, consumer_name, queue_name):
        return None

    def claim_task(self, consumer_name, queue_name, block_ms):
        self.claims += 1
        assert block_ms == CLAIM_BLOCK_MS
        if self.claims == 1:
            if self.signal_during_claim:       # SIGTERM lands while the message is in flight
                self.worker._handle_signal(signal.SIGTERM, None)
            return types.SimpleNamespace(message_id="1-0", raw_data={}, job_id=7, epoch_num=0, package_num=0)
        return None

    def keep_alive(self, task, consumer_name):
        return None

    def ack_task(self, task):
        self.acked.append(task.message_id)

    def nack_task(self, task, requeue=True, count=True):
        self.nacked.append(task.message_id)


def _worker(signal_during_claim: bool):
    w = DistributedWorker.__new__(DistributedWorker)
    w.worker_name, w._queue_name = "w-test", "q"
    w._shutdown_requested, w._processing_task = False, False
    w._last_reclaim = float('-inf')
    w.messages = _Messages(w, signal_during_claim)
    w._on_idle = lambda: None
    w.processed = []
    w._process_task = lambda task: w.processed.append(task.message_id) or True
    return w


def test_sigterm_during_a_claim_still_processes_and_acks_the_task():
    w = _worker(signal_during_claim=True)
    w._run_loop()                                  # returns instead of raising SystemExit
    assert w.processed == ["1-0"] and w.messages.acked == ["1-0"] and not w.messages.nacked
    assert w.messages.claims == 1                  # no new claim after the signal


def test_sigterm_while_idle_only_sets_the_flag():
    w = _worker(signal_during_claim=False)
    w._handle_signal(signal.SIGTERM, None)         # must not sys.exit(0) here
    assert w._shutdown_requested
    w._run_loop()
    assert w.messages.claims == 0                  # the loop checks the flag before claiming


def test_idle_shutdown_latency_is_bounded_by_the_claim_block():
    assert CLAIM_BLOCK_MS <= 1000                  # well inside the 120 s termination grace


def test_a_device_fault_hands_the_task_back_before_the_worker_exits(monkeypatch):
    w = _worker(signal_during_claim=False)
    w._last_failure_transient, w._restart_reason = False, None
    w._job_is_gone = lambda job_id: False

    def fault(task):
        w._restart_reason = "[gpu] an illegal memory access was encountered"
        return False
    w._process_task = fault

    def exit_for_restart(reason):
        raise SystemExit(reason)
    monkeypatch.setattr(DW, "exit_for_restart", exit_for_restart)
    with pytest.raises(SystemExit, match="illegal memory access"):
        w._run_loop()
    assert w.messages.nacked == ["1-0"] and w.messages.acked == []
