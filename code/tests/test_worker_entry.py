# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The start-up the worker entrypoints share (workers/entry.py): buckets,
then the metrics and liveness server, then the schema wait, and only then
the worker. A GPU worker without a usable device exits first."""
import types

import pytest

import workers.entry as E


def _record(monkeypatch, events):
    monkeypatch.setattr(E.worker_metrics, "start_metrics_server", lambda t, d: events.append(("metrics", t, d)))
    monkeypatch.setattr(E, "wait_for_job_schema", lambda: events.append("schema"))


def test_a_worker_starts_after_its_buckets_metrics_and_schema(monkeypatch):
    events = []
    _record(monkeypatch, events)
    worker = types.SimpleNamespace(run=lambda queue_name: events.append(("run", queue_name)))
    E.run_worker("test worker", "test", "distributed_v2", "cpu", "tasks",
                 lambda: events.append("worker") or worker, prepare=lambda: events.append("buckets"))
    assert events == ["buckets", ("metrics", "distributed_v2", "cpu"), "schema", "worker", ("run", "tasks")]


def test_a_gpu_worker_without_a_device_exits_before_anything_else(monkeypatch):
    events = []
    _record(monkeypatch, events)
    monkeypatch.setattr(E, "GPU_AVAILABLE", False)
    with pytest.raises(SystemExit):
        E.run_worker("test worker", "test", "monolithic", "gpu", "q", lambda: events.append("worker"),
                     prepare=lambda: events.append("buckets"))
    assert events == []
