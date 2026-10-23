# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Timing and diagnostics instrumentation of the V2 pipeline.

Round-trip counting, per-task / per-merge timing records, snapshot energy
diagnostics and the job start marker. Uses the
live-Redis fixtures of conftest.py and the job driver of helpers.py.
"""
from __future__ import annotations

import json

import pytest

import task_timing
from helpers import N_BODIES, N_EPOCHS, create_job, drive, requires_redis, v2_worker
from v2.binary_format import BODY_SIZE
from v2.redis_service import rt_count, rt_reset


pytestmark = requires_redis


@pytest.fixture
def timing_on(monkeypatch):
    monkeypatch.setattr(task_timing, "TIMING_ENABLED", True)
    monkeypatch.setattr(task_timing, "DIAGNOSTICS_ENABLED", True)


def _records(svc, job_id=1):
    return [json.loads(x) for x in svc.client.lrange(f"v2:timings:{job_id}", 0, -1)]


def test_round_trips_are_counted_per_thread(svc):
    rt_reset()
    svc.client.set("a", 1)
    svc.client.get("a")
    pipe = svc.client.pipeline()
    pipe.get("a")
    pipe.get("a")
    pipe.execute()
    svc.set_job_meta(9, {"status": "IN_PROGRESS"})   # HSET + EXPIRE
    assert rt_count() == 2 + 1 + 2


def test_a_pipelines_script_check_is_a_counted_round_trip(svc):
    """A pipeline holding a script sends SCRIPT EXISTS on its own before
    execute(): one more round trip, which the counter counts."""
    script = svc.client.register_script("return 1")

    def run_in_pipeline():
        rt_reset()
        pipe = svc.client.pipeline()
        script(keys=[], args=[], client=pipe)
        pipe.execute()
        return rt_count()
    assert run_in_pipeline() == 3                # SCRIPT EXISTS + SCRIPT LOAD + the pipeline
    assert run_in_pipeline() == 2                # cached: SCRIPT EXISTS + the pipeline


def test_merge_records_cover_their_time(svc, no_pg, timing_on):
    """The merge record names the phases that were untimed (renewer, fence,
    lease release) and reports what is still uncovered as untimed_s."""
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    m = [r for r in _records(svc) if r["kind"] == "merge"]
    assert m
    for r in m:
        for phase in ("begin_merge", "renewer_start", "renewer_stop", "commit"):
            assert f"{phase}_s" in r
        # the publication script releases the lease of a non-final epoch. The
        # final epoch keeps its separate steps and the separate release
        final = r["epoch"] == N_EPOCHS
        assert ("release_s" in r) == final and ("publish_s" in r) != final
        phases = sum(v for k, v in r.items() if k.endswith("_s") and k not in ("total_s", "untimed_s"))
        assert abs(r["total_s"] - phases - r["untimed_s"]) < 1e-9
        assert r["untimed_s"] >= 0


@pytest.mark.parametrize("method", ["symplectic_euler", "grk4_linear", "grk4_accel"])
def test_round_trip_budget(svc, no_pg, timing_on, method):
    """The epoch's Redis round trips, fixed so a change shows up here: a
    task is one pipeline (fence, state, a(0)) and the registration. A merge is
    begin_merge, the commit (which installs the staged a(0) itself) and the
    publication script. On the final epoch the merge keeps its separate
    cleanup, finish and lease release, and snapshot epochs add the snapshot
    inputs."""
    task_rt, merge_rt = 2, 3
    create_job(svc, method=method)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    recs = _records(svc)
    tasks = [r for r in recs if r["kind"] == "task" and r["epoch"] > 1]
    merges = [r for r in recs if r["kind"] == "merge" and 1 < r["epoch"] < N_EPOCHS]
    assert tasks and merges
    assert {r["redis_rt"] for r in tasks} == {task_rt}
    assert {r["redis_rt"] for r in merges} == {merge_rt}
    # the merger neither reads nor uploads the state
    assert {r.get("bytes_in", 0) for r in merges} == {0}
    assert {r.get("bytes_out", 0) for r in merges} == {0}
    assert {r["bytes_staged"] for r in merges} == {N_BODIES * 64}


def test_no_records_without_the_flag(svc, no_pg):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    assert _records(svc) == []
    assert svc.client.hlen("v2:diag:1") == 0


def test_task_and_merge_records_and_diagnostics(svc, no_pg, timing_on):
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    recs = _records(svc)
    tasks = [r for r in recs if r["kind"] == "task"]
    merges = [r for r in recs if r["kind"] == "merge"]
    assert len(tasks) == 3 * N_EPOCHS and len(merges) == N_EPOCHS
    t = tasks[0]
    for key in ("fetch_state_s", "decode_state_s", "compute_s", "encode_s", "register_s", "total_s"):
        assert key in t and t[key] >= 0
    assert t["bytes_state_in"] == N_BODIES * BODY_SIZE
    assert t["bytes_out"] == t["n_local"] * BODY_SIZE
    assert t["redis_rt"] >= 3 and t["P"] == 3
    assert t["compute_s"] <= t["total_s"]
    m = merges[0]
    for key in ("begin_merge_s", "commit_s", "publish_s"):
        assert key in m
    assert m["bytes_staged"] == N_BODIES * BODY_SIZE
    assert m["path"] == "merge" and m["takeover"] is False
    assert merges[-1].get("finalize_s") is not None

    diag = {int(k): json.loads(v) for k, v in svc.client.hgetall("v2:diag:1").items()}
    assert sorted(diag) == [0, N_EPOCHS]          # initial state + final epoch
    e0, e1 = diag[0]["energy"], diag[N_EPOCHS]["energy"]
    assert abs(e1 - e0) / abs(e0) < 1e-3            # a few rk4 steps
    assert "diagnostics_s" in merges[0] and "diagnostics_s" in merges[-1]


def test_start_is_marked_once(svc, no_pg):
    svc.set_job_meta(5, {"status": "IN_PROGRESS"})
    assert svc.mark_started(5)
    assert not svc.mark_started(5)
    assert float(svc.get_job_meta(5)["started_at"]) > 0

    # a meta that is gone (job cleaned up or expired) is not recreated
    assert not svc.mark_started(6)
    assert not svc.client.exists("v2:job:6:meta")
