# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Tests for the array-based snapshot CSV writer and background uploader.

The array-based writer produces the snapshot columns in row order (sorted by
n), with full float64 round-trip precision.
"""
import csv
import io

import numpy as np
import pytest

import worker_monolithic_base as wmb
from worker_monolithic_base import SnapshotUploader, _snapshot_csv_from_arrays


def _make_arrays(n=5, seed=7):
    rng = np.random.default_rng(seed)
    positions = rng.standard_normal((n, 3))
    velocities = rng.standard_normal((n, 3))
    masses = np.abs(rng.standard_normal(n)) + 0.1
    ids = np.arange(100, 100 + n, dtype=np.int32)
    return positions, velocities, masses, ids


class TestSnapshotCsvFromArrays:
    def test_columns_and_values_roundtrip(self):
        positions, velocities, masses, ids = _make_arrays()
        buffer = _snapshot_csv_from_arrays(42, ids, masses, positions, velocities)

        rows = list(csv.DictReader(io.StringIO(buffer.getvalue())))
        assert len(rows) == len(ids)
        assert list(rows[0].keys()) == ['epoch', 'n', 'idBody', 'm', 'x', 'y', 'z', 'vx', 'vy', 'vz']

        for i, row in enumerate(rows):
            assert int(row['epoch']) == 42
            assert int(row['n']) == i
            assert int(row['idBody']) == ids[i]
            # %.17g must round-trip float64 exactly
            assert float(row['m']) == masses[i]
            assert float(row['x']) == positions[i, 0]
            assert float(row['vz']) == velocities[i, 2]

    def test_acceleration_columns_for_velocity_verlet(self):
        positions, velocities, masses, ids = _make_arrays()
        accelerations = np.random.default_rng(3).standard_normal((len(ids), 3))
        buffer = _snapshot_csv_from_arrays(
            7, ids, masses, positions, velocities, accelerations
        )

        rows = list(csv.DictReader(io.StringIO(buffer.getvalue())))
        assert 'ax' in rows[0] and 'az' in rows[0]
        for i, row in enumerate(rows):
            assert float(row['ax']) == accelerations[i, 0]
            assert float(row['az']) == accelerations[i, 2]

    def test_rows_sorted_by_n(self):
        positions, velocities, masses, ids = _make_arrays(4)
        ns = np.array([3, 0, 2, 1], dtype=np.int64)
        buffer = _snapshot_csv_from_arrays(
            1, ids, masses, positions, velocities, ns=ns
        )

        rows = list(csv.DictReader(io.StringIO(buffer.getvalue())))
        assert [int(r['n']) for r in rows] == [0, 1, 2, 3]
        # Row written for n=0 must carry the data of source index 1
        assert int(rows[0]['idBody']) == ids[1]
        assert float(rows[0]['x']) == positions[1, 0]


class TestSnapshotUploader:
    def test_upload_error_raised_on_drain_then_cleared(self, monkeypatch):
        calls = []

        def failing_upload(bucket, key, buffer):
            calls.append(key)
            raise RuntimeError("s3 down")

        monkeypatch.setattr(wmb, 'upload_file', failing_upload)
        uploader = SnapshotUploader()
        try:
            uploader.submit('bucket', 'key-1', io.StringIO('x'))
            with pytest.raises(RuntimeError, match='s3 down'):
                uploader.drain()
            # Error is cleared after being surfaced — next job is unaffected
            uploader.drain()
        finally:
            uploader.close()

    def test_uploads_complete_before_drain_returns(self, monkeypatch):
        uploaded = []
        monkeypatch.setattr(
            wmb, 'upload_file', lambda bucket, key, buf: uploaded.append(key)
        )
        uploader = SnapshotUploader()
        try:
            for i in range(5):
                uploader.submit('bucket', f'key-{i}', io.StringIO('x'))
            uploader.drain()
            assert uploaded == [f'key-{i}' for i in range(5)]
        finally:
            uploader.close()

    def test_settle_drops_an_ended_jobs_error(self, monkeypatch):
        """A job that ended without drain() (pause, cancel, failure) must not
        leave its upload error to fail the next job's first submit()."""

        def failing_upload(bucket, key, buffer):
            raise RuntimeError("s3 down")

        monkeypatch.setattr(wmb, 'upload_file', failing_upload)
        uploader = SnapshotUploader()
        try:
            uploader.submit('bucket', 'job-1/key', io.StringIO('x'))
            uploader.settle()                     # job 1 paused
            monkeypatch.setattr(wmb, 'upload_file', lambda bucket, key, buf: None)
            uploader.submit('bucket', 'job-2/key', io.StringIO('x'))   # no stale error
            uploader.drain()
        finally:
            uploader.close()
