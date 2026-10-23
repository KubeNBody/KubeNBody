# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""V2 snapshot storage (v2/file_service_v2.py) over in-memory S3 clients."""
from __future__ import annotations

import csv
import io
import json
import threading
from concurrent.futures import CancelledError

import numpy as np
import pytest
from botocore.exceptions import ClientError

import v2.file_service_v2 as fs
from helpers import patch_where_used
from v2.binary_format import BODY_SIZE, BODY_STRUCT, numpy_to_binary
from v2.file_service_v2 import final_results_csv


class _S3:
    """In-memory S3 client (the calls file_service_v2 makes)."""

    def __init__(self, objects, fail_get=(), outage=0):
        self.objects, self.fail_get = dict(objects), set(fail_get)
        self.outage = outage            # this many reads fail with a server fault first

    def get_object(self, Bucket, Key):
        if self.outage:
            self.outage -= 1
            raise ClientError({"Error": {"Code": "SlowDown", "Message": "SlowDown"}}, "GetObject")
        if Key in self.fail_get:
            raise RuntimeError(f"cannot read {Key}")
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def list_objects_v2(self, Bucket, Prefix, **kw):
        return {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def delete_objects(self, Bucket, Delete):
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)
        return {}


def _sidecar(monkeypatch, **s3_kw):
    json_key, accel_key = fs.get_snapshot_aux_keys(1, 2)
    s3 = _S3({json_key: json.dumps({"epoch": 2, "next_dt": "0.0125", "accelerations": True,
                                    "simulation_time": "1.5", "dt_used": "0.0124"}).encode(),
              accel_key: b"a" * 24}, **s3_kw)
    patch_where_used(monkeypatch, fs, "get_s3_client", lambda: s3)
    monkeypatch.setattr(fs.time, "sleep", lambda s: None)
    return s3, json_key, accel_key


# One read gives everything a recovery needs from the sidecar
def test_snapshot_aux_reads_the_step_the_clock_and_the_accelerations(monkeypatch):
    _sidecar(monkeypatch)
    assert fs.download_snapshot_aux(1, 2) == fs.SnapshotAux(0.0125, b"a" * 24, 1.5, 0.0124)


# A failed a(0) read still returns the snapshot's next step.
def test_snapshot_aux_returns_next_dt_when_accelerations_fail(monkeypatch):
    _sidecar(monkeypatch, fail_get={fs.get_snapshot_aux_keys(1, 2)[1]})
    aux = fs.download_snapshot_aux(1, 2)
    assert aux.next_dt == 0.0125 and aux.accelerations is None and aux.simulation_time == 1.5


# A snapshot without a sidecar (written before sidecars) recovers at the base step
def test_snapshot_aux_of_a_snapshot_without_one_is_empty(monkeypatch):
    s3, json_key, _ = _sidecar(monkeypatch)
    del s3.objects[json_key]
    assert fs.download_snapshot_aux(1, 2) == fs.SnapshotAux()


# An S3 outage is retried, and one that outlasts the retries raises instead of
# recovering without the step and the clock
def test_snapshot_aux_retries_an_outage_and_raises_when_it_lasts(monkeypatch):
    _sidecar(monkeypatch, outage=1)
    assert fs.download_snapshot_aux(1, 2).simulation_time == 1.5
    _sidecar(monkeypatch, outage=10)
    with pytest.raises(ClientError):
        fs.download_snapshot_aux(1, 2)


# Deleting a job's snapshots deletes their sidecars too.
def test_delete_snapshots_removes_the_sidecars(monkeypatch):
    keys = [fs.get_snapshot_key(1, 2), *fs.get_snapshot_aux_keys(1, 2), fs.get_final_results_key(1),
            fs.get_snapshot_key(12, 2)]
    s3 = _S3({k: b"x" for k in keys})
    patch_where_used(monkeypatch, fs, "get_s3_client", lambda: s3)
    fs.delete_snapshots(1)
    assert sorted(s3.objects) == sorted([fs.get_final_results_key(1), fs.get_snapshot_key(12, 2)])


# --------------------------------------------------------------------------- #
# Uploads fenced on the generation
# --------------------------------------------------------------------------- #

# A snapshot upload delayed past a recovery must not write: otherwise it can
# interleave with the new generation's upload of the same epoch and pair one
# run's sidecar with the other run's snapshot.


class _RecordingS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append((kw["Key"], kw.get("Metadata")))


@pytest.fixture
def s3(monkeypatch):
    client = _RecordingS3()
    patch_where_used(monkeypatch, fs, "get_s3_client", lambda: client)
    return client


def test_stale_generation_snapshot_is_not_written(s3):
    fs.upload_snapshot_async(7, 50, b"x" * 64, next_dt=0.01, accelerations=b"a" * 24,
                             generation=0, is_current=lambda: False).result(timeout=10)
    assert s3.puts == []


def test_generation_turning_stale_between_sidecar_and_snapshot(s3):
    answers = iter([True, False])
    fs.upload_snapshot_async(7, 50, b"x" * 64, next_dt=0.01, generation=0,
                             is_current=lambda: next(answers)).result(timeout=10)
    keys = [k for k, _ in s3.puts]
    assert keys == ["v2/job_id/7/aux_50.json"]          # the snapshot itself is not listed


def test_current_generation_writes_sidecar_first_with_generation(s3):
    fs.upload_snapshot_async(7, 50, b"x" * 64, next_dt=0.01, accelerations=b"a" * 24,
                             generation=3, is_current=lambda: True).result(timeout=10)
    keys = [k for k, _ in s3.puts]
    assert keys == ["v2/job_id/7/accel_50.bin", "v2/job_id/7/aux_50.json", "v2/job_id/7/snapshot_50.bin"]
    assert s3.puts[-1][1] == {"generation": "3"}


def binary_to_bodies(data):
    """Per-body dicts of a packed state, as the old per-row writer read them."""
    keys = ("n", "idBody", "m", "x", "y", "z", "vx", "vy", "vz")
    return [dict(zip(keys, BODY_STRUCT.unpack_from(data, i * BODY_SIZE))) for i in range(len(data) // BODY_SIZE)]


def test_final_results_csv_is_byte_identical_to_the_dict_writer():
    """The vectorised writer must produce exactly what csv.DictWriter wrote from
    per-body dicts (readers parse this file, the bytes should not change)."""
    rng = np.random.default_rng(3)
    n = 257
    pos = rng.normal(size=(n, 3)) * np.array([1e-300, 1.0, 1e300])
    vel = rng.normal(size=(n, 3))
    vel[0] = [0.1, -0.0, 1 / 3]
    data = numpy_to_binary(pos, vel, rng.uniform(1e-9, 1.0, n), np.arange(7, 7 + n, dtype=np.int32))
    bodies = binary_to_bodies(data)
    for b in bodies:
        b["epoch"] = 42
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=["epoch"] + [k for k in bodies[0] if k != "epoch"])
    w.writeheader()
    w.writerows(bodies)
    assert final_results_csv(data, 42) == out.getvalue()


def test_snapshot_uploads_retry_transient_errors_only(monkeypatch):
    """The sidecar (written first) had no retry, so one transient error lost
    the whole snapshot. A permanent error is not retried at all."""
    calls = []

    class S3:
        def __init__(self, failures):
            self.failures = list(failures)

        def put_object(self, Bucket, Key, Body, **kw):
            calls.append(Key)
            if self.failures:
                code = self.failures.pop(0)
                raise ClientError({"Error": {"Code": code, "Message": code}}, "PutObject")
    monkeypatch.setattr("time.sleep", lambda s: None)
    patch_where_used(monkeypatch, fs, "get_s3_client", lambda: S3(["SlowDown"]))
    fs.upload_snapshot_aux(1, 2, next_dt=0.01)
    assert len(calls) == 2                                  # retried once, then stored
    calls.clear()
    patch_where_used(monkeypatch, fs, "get_s3_client", lambda: S3(["AccessDenied"]))
    with pytest.raises(ClientError):
        fs.upload_snapshot(1, 2, b"x" * 64)
    assert len(calls) == 1


def test_queued_snapshot_uploads_are_bounded(s3, monkeypatch):
    """During an S3 outage every upload hangs until its retries give up. An
    unbounded queue of full states grew until the worker was killed. Past the
    bound the oldest queued droppable snapshot is dropped, never a final one."""
    release, started = threading.Event(), threading.Semaphore(0)

    def hang(*a, **k):
        started.release()
        release.wait(10)
    monkeypatch.setattr(fs, "upload_snapshot", hang)
    monkeypatch.setattr(fs, "_pending_uploads", fs.collections.deque())
    futures = [fs.upload_snapshot_async(7, e, b"x" * 64, droppable=e != 30) for e in (10, 20)]
    for _ in range(2):              # both executor threads are busy uploading
        assert started.acquire(timeout=10)
    futures += [fs.upload_snapshot_async(7, e, b"x" * 64, droppable=e != 30) for e in (30, 40, 50, 60)]
    release.set()
    for f in futures:
        try:
            f.result(timeout=10)
        except CancelledError:
            pass
    # 10 and 20 were uploading, 30 is final: the queued 40 and then 50 go
    assert [f.nbody_epoch for f in futures if f.cancelled()] == [40, 50]


def test_a_jobs_only_queued_snapshot_is_not_dropped_for_another_job(s3, monkeypatch):
    """The queue is shared by every job a worker merges: a snapshot of job B
    dropped job A's only (so newest) queued snapshot. Only a snapshot its own
    job has a newer upload for is superseded."""
    release, started = threading.Event(), threading.Semaphore(0)

    def hang(*a, **k):
        started.release()
        release.wait(10)
    monkeypatch.setattr(fs, "upload_snapshot", hang)
    monkeypatch.setattr(fs, "_pending_uploads", fs.collections.deque())
    futures = [fs.upload_snapshot_async(1, e, b"x" * 64, droppable=True) for e in (10, 20)]
    for _ in range(2):
        assert started.acquire(timeout=10)
    # job 2's 10, job 3's 10, then job 4's 10: no job has two queued snapshots
    futures += [fs.upload_snapshot_async(job, 10, b"x" * 64, droppable=True) for job in (2, 3, 4)]
    release.set()
    for f in futures:
        try:
            f.result(timeout=10)
        except CancelledError:
            pass
    assert not any(f.cancelled() for f in futures)


def test_finished_uploads_behind_a_slow_one_do_not_count(s3, monkeypatch):
    """Only finished uploads at the head were pruned: behind one slow upload
    the finished ones kept the queue full while S3 was healthy."""
    slow, started = threading.Event(), threading.Semaphore(0)

    def upload(job_id, epoch, *a, **k):
        if epoch == 10:
            started.release()
            slow.wait(10)
    monkeypatch.setattr(fs, "upload_snapshot", upload)
    monkeypatch.setattr(fs, "_pending_uploads", fs.collections.deque())
    head = fs.upload_snapshot_async(1, 10, b"x" * 64, droppable=True)
    assert started.acquire(timeout=10)
    quick = [fs.upload_snapshot_async(1, e, b"x" * 64, droppable=True) for e in (20, 30, 40)]
    for f in quick:
        f.result(timeout=10)
    fs.upload_snapshot_async(1, 50, b"x" * 64, droppable=True)
    # the finished 20, 30 and 40 behind the running 10 no longer count
    assert [f.nbody_epoch for f in fs._pending_uploads] == [10, 50]
    slow.set()
    head.result(timeout=10)


def test_a_failed_snapshot_listing_is_not_an_empty_one(monkeypatch):
    """Any ClientError read as "no snapshots": a completed job's metrics were
    then computed from nothing and cached for good."""

    class S3:
        def __init__(self, code):
            self.code = code

        def list_objects_v2(self, **kw):
            raise ClientError({"Error": {"Code": self.code}}, "ListObjectsV2")
    patch_where_used(monkeypatch, fs, "get_s3_client", lambda: S3("SlowDown"))
    with pytest.raises(ClientError):
        fs.list_snapshots(7)
    patch_where_used(monkeypatch, fs, "get_s3_client", lambda: S3("NoSuchBucket"))
    assert fs.list_snapshots(7) == []


def test_an_old_generations_upload_does_not_supersede_the_new_ones(s3, monkeypatch):
    """After a recovery to an earlier epoch, the old generation's epoch-100
    upload made the new generation's epoch-60 snapshot look superseded. The
    old one is skipped as stale, so the newest valid snapshot was lost."""
    release, started = threading.Event(), threading.Semaphore(0)

    def hang(*a, **k):
        started.release()
        release.wait(10)
    monkeypatch.setattr(fs, "upload_snapshot", hang)
    monkeypatch.setattr(fs, "_pending_uploads", fs.collections.deque())
    busy = [fs.upload_snapshot_async(job, 10, b"x" * 64, droppable=True, generation=0) for job in (8, 9)]
    for _ in range(2):
        assert started.acquire(timeout=10)
    other = fs.upload_snapshot_async(5, 10, b"x" * 64, droppable=True, generation=0)
    new_gen = fs.upload_snapshot_async(7, 60, b"x" * 64, droppable=True, generation=1)
    old_gen = fs.upload_snapshot_async(7, 100, b"x" * 64, droppable=True, generation=0)   # queue full here
    release.set()
    for f in busy + [other, new_gen, old_gen]:
        try:
            f.result(timeout=10)
        except CancelledError:
            pass
    assert not new_gen.cancelled()
