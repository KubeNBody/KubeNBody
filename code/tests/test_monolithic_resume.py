# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Resuming a monolithic job (worker_monolithic_base.load_bodies_for_job).

current_epoch is written while the newest snapshots may still wait in the
background uploader, so a crash can leave the snapshot it points at
unwritten. Resume walks back to the newest usable snapshot instead of
restarting from the input. A transient storage error is raised (the job is
retried), never taken for a missing snapshot. And the simulation time starts
from the snapshot actually resumed from.

No object storage: download_file reads a dict.
"""
from __future__ import annotations

import json
import logging
import types

import numpy as np
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

import worker_monolithic_base as W
from worker_monolithic_base import _snapshot_csv_from_arrays


N = 4
DT = 1e-3
INPUT = "n,idBody,m,x,y,z,vx,vy,vz\r\n" + "".join(
    f"{i},{i + 1},1.0,{float(i)},0.0,0.0,0.0,0.1,0.0\r\n" for i in range(N))


def _snapshot(epoch, n=N):
    pos = np.full((n, 3), float(epoch))
    return _snapshot_csv_from_arrays(epoch, np.arange(1, n + 1), np.ones(n), pos, np.zeros((n, 3))).getvalue()


def _key(epoch, name="epoch_results.csv"):
    return f"job_id/9/epoch_num/{epoch}/{name}"


@pytest.fixture
def store(monkeypatch):
    objects = {"k": INPUT}
    failing = set()

    def download(bucket, key):
        if key in failing:
            raise EndpointConnectionError(endpoint_url="http://minio:9000")
        if key not in objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return objects[key]
    monkeypatch.setattr(W, "download_file", download)
    return types.SimpleNamespace(objects=objects, failing=failing)


def _resume(current_epoch, adaptive=False, db_time=None, interval=5):
    job = types.SimpleNamespace(job_id=9, status="IN_PROGRESS", current_epoch=current_epoch,
                                adaptive_enabled=adaptive,
                                current_simulation_time=current_epoch * DT if db_time is None else db_time)
    message = types.SimpleNamespace(num_of_epochs=100, snapshot_interval=interval, bucket="b",
                                    file_key="k", time_step=DT)
    bodies, start = W.load_bodies_for_job(job, message, True, logging.getLogger("t"))
    return job, bodies, start


def test_a_snapshot_not_yet_uploaded_falls_back_to_the_one_before(store):
    for e in (5, 10, 15, 20):
        store.objects[_key(e)] = _snapshot(e)
    # the DB says epoch 27. The epoch-25 snapshot was still queued at the crash
    job, bodies, start = _resume(27)
    assert start == 21 and bodies[0]["x"] == 20.0


def test_simulation_time_starts_at_the_snapshot_resumed_from(store):
    store.objects[_key(20)] = _snapshot(20)
    job, _, start = _resume(27)                   # the DB value is that of epoch 27
    assert start == 21 and job.current_simulation_time == pytest.approx(20 * DT)


def test_no_usable_snapshot_restarts_from_the_input_at_time_zero(store):
    job, bodies, start = _resume(27, db_time=0.5)
    assert start == 1 and job.current_simulation_time == 0.0
    assert [b["x"] for b in bodies] == [0.0, 1.0, 2.0, 3.0]


def test_a_corrupt_snapshot_is_skipped(store):
    store.objects[_key(20)] = _snapshot(20)
    store.objects[_key(25)] = _snapshot(25, n=N - 1)  # a body short: not this job's state
    _, bodies, start = _resume(27)
    assert start == 21 and bodies[0]["x"] == 20.0


@pytest.mark.parametrize("failing", [_key(25), "k"])
def test_a_transient_storage_error_is_raised_not_a_restart(store, failing):
    """An outage reading the snapshot, or the input whose row count validates
    it, says nothing about the snapshot: the job is retried, not restarted."""
    store.objects[_key(25)] = _snapshot(25)
    store.failing.add(failing)
    with pytest.raises(EndpointConnectionError):
        _resume(27)


def test_adaptive_resume_prefers_a_snapshot_with_its_sidecar(store):
    """The sidecar is queued after its CSV: a crash in between leaves the
    newest CSV without one. The one before, with its sidecar, continues
    the run exactly."""
    for e in (20, 25):
        store.objects[_key(e)] = _snapshot(e)
    store.objects[_key(20, "metadata.json")] = json.dumps({"simulation_time": 0.0123, "dt_used": 4e-4})
    job, _, start = _resume(27, adaptive=True)
    assert start == 21 and job.current_simulation_time == 0.0123 and job.resume_dt == 4e-4


def test_adaptive_resume_without_any_sidecar_estimates_the_time(store):
    """Runs from before sidecars: the newest snapshot, with the time scaled
    from the DB's (epoch, time) pair and no step size to continue from."""
    store.objects[_key(25)] = _snapshot(25)
    job, _, start = _resume(27, adaptive=True, db_time=0.054)
    assert start == 26 and job.current_simulation_time == pytest.approx(0.054 * 25 / 27)
    assert job.resume_dt is None


def test_a_transient_sidecar_error_is_raised(store):
    store.objects[_key(25)] = _snapshot(25)
    store.failing.add(_key(25, "metadata.json"))
    with pytest.raises(EndpointConnectionError):
        _resume(27, adaptive=True)


def test_the_walk_back_is_bounded(store):
    store.objects[_key(1)] = _snapshot(1)
    _, _, start = _resume(W.SNAPSHOT_FALLBACK_LIMIT + 1, interval=1)
    assert start == 1                             # epoch 1 is past the limit: restart from the input
    _, _, start = _resume(W.SNAPSHOT_FALLBACK_LIMIT, interval=1)
    assert start == 2
