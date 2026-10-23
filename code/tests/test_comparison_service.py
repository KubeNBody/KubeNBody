# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Tests for the comparison service (RMSE, interpolation, alignment)."""
import asyncio
import json
import types
from datetime import datetime

import numpy as np
import pytest
from botocore.exceptions import ClientError

import async_file_service
import comparison_worker as CW
import file_service
import routes.comparison as RC
import services.comparison_cache as CC
import services.comparison_service as CS
import utils.snapshot_loader as SL
from api_schemas import ComparisonJobResponse, ComparisonJobSubmitRequest
from diagnostics import kinetic_energy, potential_energy
from helpers import patch_where_used
from physics import compute_accelerations, step_velocity_verlet
from services.comparison_service import (
    TrajectoryPairing,
    compute_all_metrics,
    compute_job_metrics_streaming,
    compute_trajectory_rmse,
    conservation_flags,
    hermite_interpolate,
    momentum_drift_percent,
    snapshot_time,
    vector_drift,
)
from utils.snapshot_loader import select_snapshot_epochs


class TestTrajectoryRMSE:
    """Test compute_trajectory_rmse."""

    def test_identical_snapshots_zero_rmse(self):
        """Identical positions/velocities should give RMSE = 0."""

        np.random.seed(99)
        n = 10
        pos = np.random.randn(n, 3).astype(np.float64)
        vel = np.random.randn(n, 3).astype(np.float64)

        pos_rmse, vel_rmse, pos_max, vel_max = compute_trajectory_rmse(
            pos, pos, vel, vel
        )

        assert pos_rmse == pytest.approx(0.0, abs=1e-15)
        assert vel_rmse == pytest.approx(0.0, abs=1e-15)
        assert pos_max == pytest.approx(0.0, abs=1e-15)
        assert vel_max == pytest.approx(0.0, abs=1e-15)

    def test_known_rmse(self):
        """RMSE for a known constant offset."""

        n = 4
        pos_a = np.zeros((n, 3), dtype=np.float64)
        pos_b = np.ones((n, 3), dtype=np.float64)
        vel_a = np.zeros((n, 3), dtype=np.float64)
        vel_b = np.zeros((n, 3), dtype=np.float64)

        pos_rmse, vel_rmse, pos_max, vel_max = compute_trajectory_rmse(
            pos_a, pos_b, vel_a, vel_b
        )

        # Each body error = sqrt(3), RMSE = sqrt(mean(3,3,3,3)) = sqrt(3)
        expected_rmse = np.sqrt(3.0)
        assert abs(pos_rmse - expected_rmse) < 1e-10
        assert abs(pos_max - expected_rmse) < 1e-10

    def test_empty_arrays(self):
        """Empty arrays should return zeros."""

        empty = np.empty((0, 3), dtype=np.float64)
        pos_rmse, vel_rmse, pos_max, vel_max = compute_trajectory_rmse(
            empty, empty, empty, empty
        )
        assert pos_rmse == 0.0
        assert vel_rmse == 0.0

    def test_single_body_error(self):
        """Single body with known displacement."""

        pos_a = np.array([[0.0, 0.0, 0.0]], dtype=np.float64)
        pos_b = np.array([[3.0, 4.0, 0.0]], dtype=np.float64)
        vel = np.zeros((1, 3), dtype=np.float64)

        pos_rmse, _, pos_max, _ = compute_trajectory_rmse(pos_a, pos_b, vel, vel)

        assert abs(pos_rmse - 5.0) < 1e-10, f"Expected RMSE=5.0, got {pos_rmse}"
        assert abs(pos_max - 5.0) < 1e-10


class TestHermiteInterpolation:
    """hermite_interpolate: exact at the ends, accurate for smooth motion."""

    def test_reproduces_the_end_states(self):
        rng = np.random.default_rng(1)
        s0 = (rng.standard_normal((3, 3)), rng.standard_normal((3, 3)), np.ones(3))
        s1 = (rng.standard_normal((3, 3)), rng.standard_normal((3, 3)), np.ones(3))
        for t, (pos, vel, _) in ((0.5, s0), (0.8, s1)):
            p, v, _ = hermite_interpolate(0.5, s0, 0.8, s1, t)
            np.testing.assert_allclose(p, pos, atol=1e-14)
            np.testing.assert_allclose(v, vel, atol=1e-12)

    def test_cubic_motion_is_exact(self):
        """x = t^3 - t: a cubic and its derivative are reproduced exactly."""
        x, v = (lambda t: t ** 3 - t), (lambda t: 3 * t ** 2 - 1)
        state = lambda t: (np.full((1, 3), x(t)), np.full((1, 3), v(t)), np.ones(1))  # noqa: E731
        p, vel, _ = hermite_interpolate(0.2, state(0.2), 0.7, state(0.7), 0.45)
        np.testing.assert_allclose(p, x(0.45), rtol=1e-12)
        np.testing.assert_allclose(vel, v(0.45), rtol=1e-12)


def _circle(t):
    """A body on the unit circle at angular speed 1."""
    pos = np.array([[np.cos(t), np.sin(t), 0.0]])
    vel = np.array([[-np.sin(t), np.cos(t), 0.0]])
    return pos, vel, np.ones(1)


async def _aiter(items):
    for item in items:
        yield item


def _pair(ref_times, cmp_times):
    async def run():
        pairing = TrajectoryPairing(_aiter([(i + 1, t, _circle(t)) for i, t in enumerate(ref_times)]))
        for i, t in enumerate(cmp_times):
            await pairing.add(i + 1, t, _circle(t))
        return pairing.result()
    return asyncio.run(run())


def test_jobs_sharing_snapshot_times_are_compared_only_there():
    """dt and 3*dt with the same snapshot interval: every third reference
    time is also a comparison time. Comparing there is exact. Linear
    interpolation in between made the same orbit look 1% of its radius away
    from itself."""
    points = _pair([0.1 * k for k in range(1, 31)], [0.3 * k for k in range(1, 11)])
    assert [round(p.simulation_time, 9) for p in points] == [round(0.3 * k, 9) for k in range(1, 11)]
    assert all(not p.interpolated and p.position_rmse < 1e-12 for p in points)


def test_a_few_coincidental_shared_times_do_not_discard_the_series():
    """Keeping only shared times whenever there were two threw most of a
    series away (an adaptive job meeting the reference grid by chance, or
    two differently subsampled grids): irregular shared times keep every
    point, the interpolated ones marked."""
    ref = [0.1 * k for k in range(1, 31)]
    cmp = sorted({0.3, 0.6} | {0.35 + 0.27 * k for k in range(10)})
    points = _pair(ref, cmp)
    exact = sum(not p.interpolated for p in points)
    assert len(points) > 20 and 2 <= exact < 5 and any(p.interpolated for p in points)


def test_subsampled_jobs_with_related_steps_still_share_times():
    """max_snapshots picks every k-th snapshot back from the last, so jobs
    with steps dt and 10*dt keep common times (evenly spaced picks of each
    job's own grid shared only a handful)."""
    ref = [e * 0.001 for e in select_snapshot_epochs(list(range(1, 10001)), 100)]
    cmp = [e * 0.01 for e in select_snapshot_epochs(list(range(1, 1001)), 100)]
    points = _pair(ref, cmp)
    assert len(points) == 100 and not any(p.interpolated for p in points)


def test_interpolated_points_are_accurate_and_marked():
    """No shared times (e.g. adaptive steps): the comparison state is
    interpolated, within O(h^4) of the true orbit, and flagged."""
    ref = [0.17 + 0.25 * k for k in range(12)]
    points = _pair(ref, [0.3 * k for k in range(1, 11)])
    assert points and all(p.interpolated for p in points)
    assert max(p.position_rmse for p in points) < 1e-4      # linear interpolation: ~1e-2
    assert max(p.velocity_rmse for p in points) < 1e-3
    assert min(p.simulation_time for p in points) > 0.3 and max(p.simulation_time for p in points) < 3.0


def test_momentum_drift_is_measured_against_the_systems_momentum_scale():
    """Initial conditions in the centre-of-mass frame have |P| at round-off.
    Dividing by it reported a 1e-15 perturbation as a drift of tens of percent."""
    rng = np.random.default_rng(3)
    n = 2000
    pos, vel, m = rng.standard_normal((n, 3)), rng.standard_normal((n, 3)), rng.uniform(0.5, 1.5, n)
    vel -= (m[:, None] * vel).sum(axis=0) / m.sum()
    later = vel * (1 + 1e-15 * rng.standard_normal((n, 3)))
    metrics = [compute_all_metrics(pos, v, m, e, softening_sq=1.0) for e, v in ((0, vel), (1, later))]
    assert momentum_drift_percent(metrics) < 1e-10
    assert conservation_flags(metrics)[0]
    shifted = vel + 0.05      # a real change: 5% of the speed scale
    metrics = [compute_all_metrics(pos, v, m, e, softening_sq=1.0) for e, v in ((0, vel), (1, shifted))]
    assert 1 < momentum_drift_percent(metrics) < 10 and not conservation_flags(metrics)[0]


def _stub_loader(monkeypatch, stream, **extra):
    """Replace, where comparison_service uses them, the snapshot loader's
    stream (and any `extra` names) with stand-ins."""
    monkeypatch.setattr(CS, "stream_job_snapshots", stream)
    monkeypatch.setattr(CS, "SnapshotReport", lambda: types.SimpleNamespace(missing=[]))
    for name, value in extra.items():
        monkeypatch.setattr(CS, name, value)


class TestEnergyComputationConsistency:
    """Verify energy computation uses same softening as physics."""

    def test_total_energy_conserved_one_step(self, kepler_circular):
        """Energy before and after one tiny Verlet step should be nearly identical."""

        pos, vel, masses, ids = kepler_circular

        E_before = (kinetic_energy(vel, masses) +
                    potential_energy(pos, masses))

        accel = compute_accelerations(pos, masses, ids)
        pos2, vel2, _ = step_velocity_verlet(pos, vel, masses, ids, accel, 0.0001)

        E_after = (kinetic_energy(vel2, masses) +
                   potential_energy(pos2, masses))

        assert abs(E_after - E_before) / abs(E_before) < 1e-8, \
            f"Energy changed by {abs(E_after - E_before):.2e} in one tiny step"

    def test_potential_energy_symmetric(self):
        """PE(A,B) should equal PE(B,A) — order shouldn't matter."""

        np.random.seed(77)
        n = 10
        pos = np.random.randn(n, 3).astype(np.float64)
        masses = np.abs(np.random.randn(n).astype(np.float64)) + 0.1

        pe1 = potential_energy(pos, masses)

        # Reverse order
        pe2 = potential_energy(pos[::-1].copy(), masses[::-1].copy())

        assert abs(pe1 - pe2) < 1e-12, \
            f"PE should be order-independent: {pe1} vs {pe2}"


def test_single_job_metrics_use_the_jobs_softening(monkeypatch):
    """The single-job metrics path
    (API and background precomputation call it without softening_sq) must
    measure the potential the job was integrated with."""


    pos = np.array([[0.0, 0, 0], [0.1, 0, 0]])
    vel, m = np.zeros((2, 3)), np.ones(2)

    async def one_snapshot(job, batch_size=10, max_snapshots=None, report=None, epochs=None):
        yield 1, (pos, vel, m)
    # a stand-in module: the real loader imports models (PostgreSQL)
    _stub_loader(monkeypatch, one_snapshot)
    job = types.SimpleNamespace(job_id=1, softening_sq=1.0, time_step=0.01, adaptive_enabled=False)
    result = asyncio.run(compute_job_metrics_streaming(job))
    assert result.metrics[0].total_energy == pytest.approx(-1 / np.sqrt(1.01), rel=1e-9)


def _streaming_metrics(monkeypatch, snapshots, initial):
    """compute_job_metrics_streaming over the given (epoch, state) list, with
    `initial` as the job's input file (None: unavailable)."""


    async def stream(job, batch_size=10, max_snapshots=None, report=None, epochs=None):
        for item in snapshots:
            yield item

    async def load_initial(job):
        return initial
    _stub_loader(monkeypatch, stream)
    monkeypatch.setattr(CS, "load_initial_state", load_initial)
    job = types.SimpleNamespace(job_id=1, softening_sq=1.0, time_step=0.01, adaptive_enabled=False)
    return asyncio.run(CS.compute_job_metrics_streaming(job))


def test_a_rotated_momentum_is_not_reported_as_conserved(monkeypatch):
    """Comparing |P| only would pass a momentum that turned by 90 degrees with
    the same magnitude."""
    pos = np.array([[0.0, 0, 0], [1.0, 0, 0]])
    m = np.ones(2)
    v0 = np.array([[1.0, 0, 0], [1.0, 0, 0]])          # P = (2, 0, 0)
    v1 = np.array([[0.0, 1, 0], [0.0, 1, 0]])          # P = (0, 2, 0): same |P|
    r = _streaming_metrics(monkeypatch, [(50, (pos, v1, m))], (pos, v0, m))
    assert not r.momentum_conserved
    assert _streaming_metrics(monkeypatch, [(50, (pos, v0, m))], (pos, v0, m)).momentum_conserved


def test_conservation_is_measured_from_the_initial_state(monkeypatch):
    """The baseline is t = 0 from the input file, not the first snapshot."""
    pos = np.array([[0.0, 0, 0], [1.0, 0, 0]])
    m = np.ones(2)
    v0 = np.array([[1.0, 0, 0], [-1.0, 0, 0]])         # P = 0
    drifted = np.array([[1.0, 0, 0], [-0.5, 0, 0]])    # P = (0.5, 0, 0) from epoch 50 on
    snaps = [(50, (pos, drifted, m)), (100, (pos, drifted, m))]
    r = _streaming_metrics(monkeypatch, snaps, (pos, v0, m))
    assert r.epochs == [0, 50, 100] and r.simulation_times[0] == 0.0
    assert not r.momentum_conserved and r.energy_drift_percent > 0
    # without the input file the first snapshot is the baseline (and is logged)
    r = _streaming_metrics(monkeypatch, snaps, None)
    assert r.epochs == [50, 100] and r.momentum_conserved


def test_cross_job_momentum_drift_uses_vectors():
    assert vector_drift((0.0, 2.0, 0.0), (2.0, 0.0, 0.0)) == pytest.approx(2 * np.sqrt(2))


def test_jobs_on_different_bodies_are_not_compared(monkeypatch):
    """Two inputs with the same N but different body IDs must not be
    differenced row by row."""
    files = {"a.csv": "n,idBody,m,x,y,z,vx,vy,vz\n1,1,1,0,0,0,0,0,0\n2,2,1,1,0,0,0,0,0\n",
             "b.csv": "n,idBody,m,x,y,z,vx,vy,vz\n1,2,1,1,0,0,0,0,0\n2,1,1,0,0,0,0,0,0\n",   # same bodies, other order
             "c.csv": "n,idBody,m,x,y,z,vx,vy,vz\n1,1,1,0,0,0,0,0,0\n2,7,1,1,0,0,0,0,0\n"}   # body 2 -> 7

    class S3:
        async def download_file(self, bucket, key):
            return files[key]
    patch_where_used(monkeypatch, async_file_service, "AsyncS3Service", S3)
    job = lambda key: types.SimpleNamespace(input_bucket="inputfiles", input_file_key=key)  # noqa: E731
    assert asyncio.run(CS.bodies_differ(job("a.csv"), job("a.csv"))) is None
    assert asyncio.run(CS.bodies_differ(job("a.csv"), job("b.csv"))) is None
    assert "different bodies" in asyncio.run(CS.bodies_differ(job("a.csv"), job("c.csv")))


def test_v2_adaptive_times_come_from_the_snapshot_sidecars(monkeypatch):
    """A V2 adaptive job's time axis comes from the V2 aux sidecars (the
    monolithic metadata.json alone would make it epoch * dt), and the
    comparison says when some time is missing."""

    sidecars = {"v2/job_id/5/aux_2.json": json.dumps({"epoch": 2, "next_dt": "0.004", "simulation_time": "0.017"}),
                "v2/job_id/5/aux_4.json": json.dumps({"epoch": 4, "next_dt": "0.004", "simulation_time": None})}

    class S3:
        async def download_files_batch(self, bucket, keys, raise_errors=False):
            return [(k, sidecars.get(k)) for k in keys]
    patch_where_used(monkeypatch, async_file_service, "AsyncS3Service", S3)
    _stub_loader(monkeypatch, None, _get_snapshot_epochs=lambda n, i: [2, 4])
    job = types.SimpleNamespace(job_id=5, job_type="distributed_v2", num_epochs=4, snapshot_interval=2,
                                time_step=0.01, adaptive_enabled=True, softening_sq=1.0)
    assert asyncio.run(CS._load_adaptive_sim_times(job)) == {2: 0.017}

    pos, m = np.array([[0.0, 0, 0], [1.0, 0, 0]]), np.ones(2)
    v = np.zeros((2, 3))

    async def stream(job, batch_size=10, max_snapshots=None, report=None, epochs=None):
        for e in (2, 4):
            yield e, (pos, v, m)

    async def no_initial(job):
        return None
    monkeypatch.setattr(CS, "stream_job_snapshots", stream)
    monkeypatch.setattr(CS, "load_initial_state", no_initial)
    r = asyncio.run(CS.compute_job_metrics_streaming(job))
    # epoch 4's time is estimated from the recorded ones (epoch * dt = 0.04
    # would put an adaptive job's snapshots out of time order)
    assert r.simulation_times == pytest.approx([0.017, 0.034]) and r.simulation_times_exact is False
    sidecars["v2/job_id/5/aux_4.json"] = json.dumps({"epoch": 4, "simulation_time": "0.025"})
    r = asyncio.run(CS.compute_job_metrics_streaming(job))
    assert r.simulation_times == [0.017, 0.025] and r.simulation_times_exact is True


def test_saved_comparison_response_allows_unlimited_snapshots():
    """A saved comparison requested without max_snapshots (None = all) must
    serialize. A required int made POST /comparison/saved 500 after creating
    the row, and every later listing of saved comparisons with it."""

    request = ComparisonJobSubmitRequest(reference_job_id=1, comparison_job_ids=[2])
    assert request.max_snapshots is None
    response = ComparisonJobResponse(
        comparison_id=1, status="PENDING", reference_job_id=1, comparison_job_ids=[2],
        max_snapshots=request.max_snapshots, created_at=datetime(2026, 10, 9), updated_at=datetime(2026, 10, 9),
    )
    assert response.max_snapshots is None


def test_cache_delete_also_removes_the_persisted_metrics(monkeypatch):
    """Clearing only the in-memory cache would let the next lookup reload the
    old result from S3."""
    store = {"job_id/7/metrics_cache/v2_max_all.json": "{}", "job_id/7/metrics_cache/v3_max_100.json": "{}",
             "job_id/7/epoch_num/50/epoch_results.csv": "x", "job_id/70/metrics_cache/v3_max_all.json": "{}"}

    def delete_by_prefix(bucket, prefix):
        gone = [k for k in store if k.startswith(prefix)]
        for k in gone:
            del store[k]
        return len(gone)
    patch_where_used(monkeypatch, file_service, "delete_files_by_prefix", delete_by_prefix)
    CC._metrics_cache[(7, None)] = ({"x": 1}, 0.0)
    r = asyncio.run(RC.invalidate_comparison_cache(7, current_user=None))
    assert "2 persisted" in r["message"] and (7, None) not in CC._metrics_cache
    assert sorted(store) == ["job_id/7/epoch_num/50/epoch_results.csv", "job_id/70/metrics_cache/v3_max_all.json"]


def test_comparison_worker_drops_a_malformed_message():
    """A malformed message is dropped instead of killing the consumer (the
    broker would redeliver it to every restarted worker)."""
    nacks = []
    ch = types.SimpleNamespace(basic_nack=lambda **k: nacks.append(k.get("requeue")),
                               basic_ack=lambda **k: None)
    CW.process_comparison(ch, types.SimpleNamespace(delivery_tag=7), None, b"{not json")
    assert nacks == [False]


def test_a_comparison_of_a_deleted_job_fails_with_a_readable_error(monkeypatch):
    """A job deleted before the worker ran it: the comparison showed peewee's
    "instance matching query does not exist: SQL: SELECT ..."."""
    comparison = types.SimpleNamespace(status="PENDING", save=lambda: None)
    monkeypatch.setattr(CW, "ComparisonJob", types.SimpleNamespace(
        get=lambda *a: comparison, comparison_id=types.SimpleNamespace(__eq__=lambda o: True)))
    class Deleted(Exception):
        pass

    def gone(*a):
        raise Deleted("instance matching query does not exist: SQL: SELECT ...")
    monkeypatch.setattr(CW, "Job", types.SimpleNamespace(job_id=types.SimpleNamespace(), get=gone,
                                                         DoesNotExist=Deleted))
    acks = []
    ch = types.SimpleNamespace(basic_ack=lambda **k: acks.append(1))
    body = json.dumps({"comparison_id": 3, "reference_job_id": 5, "comparison_job_ids": "[6]",
                       "max_snapshots": 10}).encode()
    CW.process_comparison(ch, types.SimpleNamespace(delivery_tag=1, redelivered=False), None, body)
    assert comparison.status == "FAILED" and comparison.error_message == "Job 5 was deleted"
    assert acks == [1]


def test_comparison_thread_settles_its_message_and_returns_its_connection(monkeypatch):
    """A failure before process_comparison's own handling (the database down
    at ComparisonJob.get) killed the thread without an ack or nack, and with
    prefetch 1 the worker received nothing more. Each thread also kept a
    pooled connection, exhausting the pool after ~20 comparisons."""
    closed, nacked = [], []
    monkeypatch.setattr(CW, "close_thread_connection", lambda: closed.append(1))
    monkeypatch.setattr(CW, "UNHANDLED_RETRY_DELAY_S", 0)

    def db_down(*a):
        raise ConnectionError("database unreachable")
    monkeypatch.setattr(CW, "process_comparison", db_down)
    ch = types.SimpleNamespace(basic_nack=lambda **k: nacked.append(k))
    CW._run_comparison(ch, types.SimpleNamespace(delivery_tag=7), None, b"{}")
    assert nacked == [{"delivery_tag": 7, "requeue": True}] and closed == [1]


def test_v2_adaptive_trajectories_use_the_recorded_simulation_times(monkeypatch):
    """The trajectory side read only the monolithic metadata.json, so a V2
    adaptive job was placed at epoch * initial dt: identical physics showed
    an RMSE growing with time instead of 0."""


    ref = types.SimpleNamespace(job_id=1, job_type="monolithic", time_step=0.01, adaptive_enabled=False)
    cmp = types.SimpleNamespace(job_id=2, job_type="distributed_v2", time_step=0.05, adaptive_enabled=True)
    for job in (ref, cmp):
        job.__dict__.update(status="COMPLETED", num_epochs=50, current_epoch=50, snapshot_interval=10,
                            force_algorithm="direct", barnes_hut_theta=None, softening_sq=1.0,
                            integration_method="velocity_verlet", gpu_enabled=False, filename="f",
                            description=None, input_bucket="b", input_file_key="k")
    cmp.num_epochs, cmp.snapshot_interval = 5, 1
    recorded = {e: 0.1 * e for e in range(1, 6)}          # the V2 aux sidecars
    epochs = {1: [10, 20, 30, 40, 50], 2: [1, 2, 3, 4, 5]}
    times = {1: lambda e: 0.01 * e, 2: lambda e: recorded[e]}

    async def stream(job, batch_size=10, max_snapshots=None, report=None, **_):
        for e in epochs[job.job_id]:
            yield e, _circle(times[job.job_id](e))

    async def sidecars(job):
        return dict(recorded) if job is cmp else {}

    async def nothing(*a):
        return None
    _stub_loader(monkeypatch, stream)
    monkeypatch.setattr(CS, "_load_adaptive_sim_times", sidecars)
    monkeypatch.setattr(CS, "load_initial_state", nothing)
    monkeypatch.setattr(CS, "bodies_differ", nothing)
    result = asyncio.run(CS.compute_full_comparison(ref, [cmp]))
    (c,) = result["comparisons"]
    assert c["simulation_times"] == pytest.approx([0.1, 0.2, 0.3, 0.4, 0.5])
    assert max(c["position_rmse_timeseries"]) < 1e-12 and c["interpolated_points"] == 0
    assert c["final_time_comparison"] == pytest.approx(0.5) and c["simulation_times_exact"]
    # without the input state, E0 is each job's first snapshot
    assert c["energy_initial_reference"] == c["energy_reference_timeseries"][0]


def _client_error(code):
    return ClientError({"Error": {"Code": code}}, "GetObject")


def _v1_job(status="COMPLETED"):
    return types.SimpleNamespace(job_id=9, job_type="monolithic", status=status, num_epochs=50,
                                 snapshot_interval=10, results_file_key=None)


def _stream_v1(monkeypatch, failures, status="COMPLETED", max_snapshots=None):
    """The real V1 individual-file stream with each epoch's download failing
    with failures.get(epoch), returning (yielded epochs, report)."""

    class S3:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def download(service, key, epoch):
        if epoch in failures:
            raise failures[epoch]
        return epoch, _circle(epoch)
    monkeypatch.setattr(SL, "AsyncS3Service", S3)
    monkeypatch.setattr(SL, "_download_and_parse_csv", download)

    async def run():
        report = SL.SnapshotReport()
        got = [e async for e, _ in SL.stream_job_snapshots(_v1_job(status), max_snapshots=max_snapshots,
                                                          report=report)]
        return got, report
    return asyncio.run(run())


def test_a_missing_snapshot_is_skipped_and_reported(monkeypatch):
    got, report = _stream_v1(monkeypatch, {20: _client_error("NoSuchKey")})
    assert got == [10, 30, 40, 50] and report.missing == [20]


def test_a_transient_read_error_ends_the_stream(monkeypatch):
    """Skipping it shortened the metrics (a lost final snapshot moved the
    drift to an earlier time), which were then cached for good."""
    with pytest.raises(Exception) as err:
        _stream_v1(monkeypatch, {30: _client_error("SlowDown")})
    assert "SlowDown" in str(err.value)


def test_a_running_jobs_future_snapshots_are_not_missing(monkeypatch):
    absent = _client_error("NoSuchKey")
    got, report = _stream_v1(monkeypatch, {20: absent, 40: absent, 50: absent}, status="IN_PROGRESS")
    assert got == [10, 30] and report.missing == [20]


def test_max_snapshots_spans_the_whole_run(monkeypatch):
    """The first N ended each job's series (and its drift) at a different
    simulation time when the jobs' steps differ."""
    got, _ = _stream_v1(monkeypatch, {}, max_snapshots=3)
    assert got == [10, 30, 50]
    assert select_snapshot_epochs(list(range(1, 101)), 4) == [25, 50, 75, 100]
    assert select_snapshot_epochs([5, 10], 1) == [10] and select_snapshot_epochs([5, 10], None) == [5, 10]


def _comparison_row(status):
    row = types.SimpleNamespace(status=status, saved=[])
    row.save = lambda: row.saved.append(row.status)
    return row


def _run_worker(monkeypatch, row, headers=None, compute=None, ack_fails=False):
    """process_comparison on a stub channel. Returns (acks, publishes)."""
    acks, published = [], []

    def ack(delivery_tag):
        if ack_fails:
            raise RuntimeError("connection closed")
        acks.append(delivery_tag)
    ch = types.SimpleNamespace(basic_ack=ack, basic_nack=lambda **k: None,
                               basic_publish=lambda **k: published.append(k["properties"].headers))
    model = lambda found: types.SimpleNamespace(get=lambda *a: found, DoesNotExist=LookupError,  # noqa: E731
                                                comparison_id=0, job_id=0)
    monkeypatch.setattr(CW, "ComparisonJob", model(row))
    monkeypatch.setattr(CW, "Job", model(types.SimpleNamespace()))
    monkeypatch.setattr(CW, "upload_file", lambda *a: None)

    async def comparison(ref, cmps, max_snapshots):
        if compute:
            compute()
        return {"comparisons": [{}], "skipped_jobs": []}
    monkeypatch.setattr(CW, "compute_full_comparison", comparison)
    body = json.dumps({"comparison_id": 3, "reference_job_id": 1, "comparison_job_ids": "[2]",
                       "max_snapshots": None}).encode()
    CW.process_comparison(ch, types.SimpleNamespace(delivery_tag=5, redelivered=False),
                          types.SimpleNamespace(headers=headers), body)
    return acks, published


def test_a_comparison_the_worker_keeps_dying_on_is_failed(monkeypatch):
    """A comparison redelivered after the worker died (status still COMPUTING)
    was retried at the head of the queue forever, blocking all others."""
    row = _comparison_row("COMPUTING")
    acks, published = _run_worker(monkeypatch, row)
    assert published == [{"x-crashes": 1, "x-requeued": 0}] and acks == [5] and row.status == "PENDING"
    row = _comparison_row("COMPUTING")
    acks, published = _run_worker(monkeypatch, row, headers={"x-crashes": 2})
    assert published == [] and acks == [5] and row.status == "FAILED"


def test_a_transient_requeue_that_could_not_reset_the_status_is_not_a_crash(monkeypatch):
    """The transient retry requeues after setting PENDING. When that save
    fails too (the database is the outage), the copy found COMPUTING and was
    counted as a crash, so two outages failed a comparison that never crashed."""
    row = _comparison_row("COMPUTING")
    acks, published = _run_worker(monkeypatch, row, headers={"x-retries": 1, "x-requeued": 1})
    assert published == [] and acks == [5] and row.status == "COMPLETED"
    # a crash during that attempt comes back redelivered: counted as before
    row = _comparison_row("COMPUTING")
    acks, published, method = [], [], types.SimpleNamespace(delivery_tag=5, redelivered=True)
    ch = types.SimpleNamespace(basic_ack=acks.append, basic_nack=lambda **k: None,
                               basic_publish=lambda **k: published.append(k["properties"].headers))
    model = types.SimpleNamespace(get=lambda *a: row, DoesNotExist=LookupError, comparison_id=0)
    monkeypatch.setattr(CW, "ComparisonJob", model)
    CW.process_comparison(ch, method, types.SimpleNamespace(headers={"x-requeued": 1}),
                          b'{"comparison_id": 3, "reference_job_id": 1, "comparison_job_ids": "[2]"}')
    assert published and published[0]["x-crashes"] == 1 and row.status == "PENDING"


def test_a_lost_ack_does_not_fail_a_finished_comparison(monkeypatch):
    """The ack of a comparison that completed while the broker connection
    dropped raised, and the handler overwrote COMPLETED with FAILED."""
    row = _comparison_row("PENDING")
    _run_worker(monkeypatch, row, ack_fails=True)
    assert row.status == "COMPLETED" and "FAILED" not in row.saved


def test_missing_adaptive_times_are_estimated_in_time_order():
    """epoch * initial dt for a snapshot whose time record is missing put it
    out of order once the adaptive step had shrunk: the comparison then
    interpolated far outside its interval (errors of 1e2 on a unit orbit)."""
    recorded = {10: 0.009, 20: 0.017, 40: 0.031}             # steps shrinking below dt = 0.001
    assert snapshot_time(20, recorded, 0.001) == 0.017
    assert snapshot_time(30, recorded, 0.001) == pytest.approx(0.024)    # between 20 and 40
    assert snapshot_time(5, recorded, 0.001) == pytest.approx(0.0045)    # from t = 0
    assert snapshot_time(50, recorded, 0.001) == pytest.approx(0.038)    # at the last interval's rate
    times = [snapshot_time(e, recorded, 0.001) for e in range(10, 60, 10)]
    assert times == sorted(times)
    assert snapshot_time(30, {}, 0.001) == pytest.approx(0.03)            # fixed step


def test_pairing_leaves_out_snapshots_out_of_time_order():
    async def run():
        pairing = TrajectoryPairing(_aiter([(i, t, _circle(t)) for i, t in enumerate([0.1, 0.2, 0.3], 1)]))
        for e, t in ((1, 0.1), (2, 0.3), (3, 0.25), (4, 0.3001)):    # 0.25 comes after 0.3
            await pairing.add(e, t, _circle(t))
        return pairing.result()
    points = asyncio.run(run())
    assert all(p.position_rmse < 1e-6 for p in points)


def test_a_sidecar_read_error_is_raised_not_read_as_missing(monkeypatch):
    """Any download error counted as a missing time record: an S3 blip gave
    a cached result estimated times."""


    class S3:
        async def download_files_batch(self, bucket, keys, raise_errors=False):
            assert raise_errors
            raise ClientError({"Error": {"Code": "SlowDown"}}, "GetObject")
    patch_where_used(monkeypatch, async_file_service, "AsyncS3Service", S3)
    _stub_loader(monkeypatch, None, _get_snapshot_epochs=lambda n, i: [2, 4])
    job = types.SimpleNamespace(job_id=5, job_type="distributed_v2", num_epochs=4, snapshot_interval=2,
                                time_step=0.01, adaptive_enabled=True)
    with pytest.raises(ClientError):
        asyncio.run(CS._load_adaptive_sim_times(job))


def test_max_snapshots_selects_the_reference_and_reads_the_other_job_at_its_times(monkeypatch):
    """Each job subsampled on its own grid: two jobs on the same grid ending
    at t = 10 and t = 7 kept 10 of 70 points."""


    def job(job_id, num_epochs):
        return types.SimpleNamespace(
            job_id=job_id, job_type="monolithic", status="COMPLETED", num_epochs=num_epochs, current_epoch=num_epochs,
            snapshot_interval=10, time_step=0.001, adaptive_enabled=False, force_algorithm="direct",
            barnes_hut_theta=None, softening_sq=1.0, integration_method="velocity_verlet", gpu_enabled=False,
            filename="f", description=None, input_bucket="b", input_file_key="k")
    ref, cmp = job(1, 10000), job(2, 7000)
    streamed = {}

    async def stream(job, batch_size=10, max_snapshots=None, report=None, epochs=None):
        planned = list(range(10, job.num_epochs + 1, 10))
        chosen = [e for e in planned if e in set(epochs)] if epochs is not None \
            else select_snapshot_epochs(planned, max_snapshots)
        streamed.setdefault(job.job_id, []).append(len(chosen))
        for e in chosen:
            yield e, _circle(e * 0.001)

    async def nothing(*a):
        return None
    _stub_loader(monkeypatch, stream)
    monkeypatch.setattr(CS, "load_initial_state", nothing)
    monkeypatch.setattr(CS, "bodies_differ", nothing)
    (c,) = asyncio.run(CS.compute_full_comparison(ref, [cmp], max_snapshots=100))["comparisons"]
    assert c["epochs_compared"] == 70 and c["interpolated_points"] == 0
    assert max(streamed[2]) <= 71          # the comparison job is read only at the reference's times
