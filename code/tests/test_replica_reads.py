# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Replica state reads (NBODY_V2_REPLICA_READS), against real redis-servers: a
primary and a replica of it (REPLICAOF)."""
from __future__ import annotations

import json
import socket
import subprocess
import time

import pytest

import task_timing
import v2.redis_service as rs
from helpers import REDIS_SERVER, create_job, drive, reference_run, requires_redis, store_accelerations, v2_worker


pytestmark = requires_redis

try:
    import redis
except ImportError:      # pragma: no cover
    redis = None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def replica(redis_port, tmp_path):
    """A redis-server replicating the test primary, in sync."""
    # the full sync otherwise waits 5 s for more replicas (diskless sync)
    redis.Redis(port=redis_port).config_set("repl-diskless-sync-delay", 0)
    port = _free_port()
    # --dir: the replica writes the RDB of the full sync there, not into the cwd
    proc = subprocess.Popen(
        [REDIS_SERVER, "--port", str(port), "--save", "", "--appendonly", "no",
         "--dir", str(tmp_path), "--replicaof", "127.0.0.1", str(redis_port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    client = redis.Redis(port=port, decode_responses=False)
    for _ in range(200):
        try:
            if client.info("replication").get("master_link_status") == "up":
                break
        except redis.ConnectionError:
            pass
        time.sleep(0.05)
    else:
        proc.terminate()
        pytest.skip("replica did not sync")
    yield client
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def replica_reads(monkeypatch):
    monkeypatch.setattr(rs.cfg, "replica_reads", True)


def _synced(svc):
    assert svc.client.execute_command("WAIT", 1, 2000) >= 1


# --------------------------------------------------------------------------- #
# get_task_inputs_from_replica
# --------------------------------------------------------------------------- #

def test_a_synced_replica_serves_the_committed_state(svc, replica, replica_reads):
    create_job(svc)
    svc.replica_client = replica
    _synced(svc)
    got = svc.get_task_inputs_from_replica(1, 0, 1)
    assert got is not None
    fields, bodies, accels = got
    primary = svc.get_task_inputs(1)
    assert bodies == primary[1] and fields == primary[0] and accels is None


def test_replica_reads_are_off_by_default(svc, replica):
    create_job(svc)
    svc.replica_client = replica
    _synced(svc)
    assert svc.get_task_inputs_from_replica(1, 0, 1) is None


def test_a_replica_state_of_another_epoch_or_generation_is_not_used(svc, replica, replica_reads):
    create_job(svc)
    svc.replica_client = replica
    _synced(svc)
    assert svc.get_task_inputs_from_replica(1, 0, 2) is None      # it holds epoch 0's state
    assert svc.get_task_inputs_from_replica(1, 5, 1) is None      # another generation


def test_a_lagging_replica_falls_back_to_the_primary(svc, replica, replica_reads):
    """The primary committed epoch 1 but the replica stopped replicating: a
    task of epoch 2 must not compute from the replica's epoch-0 state."""
    create_job(svc)
    svc.replica_client = replica
    _synced(svc)
    replica.execute_command("REPLICAOF", "NO", "ONE")
    svc.client.hset("v2:job:1:meta", "committed_epoch", "1")
    svc.client.set("v2:bodies:1", b"epoch-1 state")
    assert svc.get_task_inputs_from_replica(1, 0, 2) is None


def test_a_missing_a0_on_the_replica_falls_back(svc, replica, replica_reads):
    """a(0) is shipped after the commit and is not waited for: absent on the
    replica, the primary is read (computing a(0) locally instead would change
    the result of a returned-accelerations job)."""
    create_job(svc)
    svc.replica_client = replica
    _synced(svc)
    assert svc.get_task_inputs_from_replica(1, 0, 1, want_accels=True) is None
    store_accelerations(svc, 1, b"a0")
    _synced(svc)
    assert svc.get_task_inputs_from_replica(1, 0, 1, want_accels=True)[2] == b"a0"


def test_an_unreachable_replica_is_left_alone_for_a_while(svc, replica_reads, monkeypatch):
    create_job(svc)
    svc.replica_client = redis.Redis(port=_free_port(), socket_connect_timeout=0.2)
    assert svc.get_task_inputs_from_replica(1, 0, 1) is None
    calls = []
    monkeypatch.setattr(svc.replica_client, "pipeline", lambda *a, **k: calls.append(1))
    assert svc.get_task_inputs_from_replica(1, 0, 1) is None
    assert calls == []                      # within REPLICA_RETRY_AFTER_S


def test_a_job_read_partly_from_a_replica_ends_bitwise_as_without_it(svc, replica, replica_reads,
                                                                    no_pg, monkeypatch):
    """Odd packages read the state from the replica; commits wait for it (as
    under Sentinel), so it holds each epoch's state when the tasks run."""
    ref = reference_run(svc)
    monkeypatch.setattr(task_timing, "TIMING_ENABLED", True)
    svc.replica_client = replica
    svc._use_sentinel = True                 # commit + WAIT 1, as in production
    create_job(svc)
    w, backend, finals = v2_worker(svc)
    drive(w, backend, svc)
    assert finals["state"] == ref
    tasks = [json.loads(x) for x in svc.client.lrange("v2:timings:1", 0, -1)]
    tasks = [r for r in tasks if r["kind"] == "task"]
    odd = [r for r in tasks if r["package_num"] % 2 == 1]
    assert sum(r.get("replica_reads", 0) for r in odd) >= 1
    assert all(r.get("replica_reads", 0) + r.get("replica_fallbacks", 0) == 1 for r in odd)
    assert not any(r.get("replica_reads") for r in tasks if r["package_num"] % 2 == 0)
