# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Results exports and animation pages bounded in memory: snapshots and
combined CSVs are streamed, not held whole, and an animation page is cut to
a particle budget (routes/results.py, v2/api_v2.py, animation_cache.py)."""
from __future__ import annotations

import asyncio
import io
import threading
import time
import types

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi import HTTPException

import animation_cache as AC
import routes.results as R
import v2.api_v2 as A
import v2.binary_format as BF
import v2.file_service_v2 as FS
from animation_cache import PAGE_PARTICLE_BUDGET, effective_page_limit
from helpers import patch_where_used


def _drain(response):
    """The body of a StreamingResponse, as its list of chunks."""
    async def collect():
        out = []
        async for chunk in response.body_iterator:
            out.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        return out
    return asyncio.run(collect())


class _Body:
    """botocore StreamingBody subset over bytes."""

    def __init__(self, data: bytes):
        self._f = io.BytesIO(data)
        self.closed = False

    def read(self):
        return self._f.read()

    def iter_chunks(self, chunk_size=1024):
        while chunk := self._f.read(chunk_size):
            yield chunk

    def iter_lines(self, chunk_size=1024):
        pending = b""
        for chunk in self.iter_chunks(chunk_size):
            lines = (pending + chunk).splitlines(True)
            for line in lines[:-1]:
                yield line.splitlines()[0]
            pending = lines[-1]
        if pending:
            yield pending.splitlines()[0]

    def close(self):
        self.closed = True


# --------------------------------------------------------------------------- #
# v1: concatenated per-epoch snapshots
# --------------------------------------------------------------------------- #

def _old_concatenation(contents):
    """What _concatenated_snapshots returned before it streamed."""
    combined = []
    for content in contents:
        lines = [line.strip() for line in content.strip().split('\n') if line.strip()]
        if lines:
            combined.extend(lines if not combined else lines[1:])
    return '\n'.join(combined) + '\n'


def _fake_async_s3(files, stats):
    class Service:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def download_file(self, bucket, key):
            stats["live"] += 1
            stats["peak"] = max(stats["peak"], stats["live"])
            await asyncio.sleep(0.001)
            stats["live"] -= 1
            return files[key]
    return Service


def test_concatenated_snapshots_stream_with_bounded_prefetch(monkeypatch):
    epochs = list(range(1, 21))
    contents = {e: "x,y,z,m\r\n" + "".join(f"{e},{i},0,1\r\n" for i in range(3)) for e in epochs}
    contents[5] = ""                          # an empty file is skipped
    contents[6] = "x,y,z,m\n"                 # a header-only file adds nothing
    files = {f"job_id/7/epoch_num/{e}/epoch_results.csv": c for e, c in contents.items()}
    stats = {"live": 0, "peak": 0}
    monkeypatch.setattr(R, "AsyncS3Service", _fake_async_s3(files, stats))

    async def run():
        r = await R._concatenated_snapshots_response(7, epochs, "job-7-full-history.csv")
        chunks = []
        async for c in r.body_iterator:
            chunks.append(c)
        return r, chunks
    response, chunks = asyncio.run(run())
    assert b"".join(chunks).decode() == _old_concatenation([contents[e] for e in epochs])
    assert len(chunks) > 1 and stats["peak"] <= R.SNAPSHOT_PREFETCH
    assert response.headers["content-disposition"] == "attachment; filename=job-7-full-history.csv"
    assert response.media_type == "text/csv"


def test_concatenation_of_empty_snapshots_is_404(monkeypatch):
    files = {f"job_id/7/epoch_num/{e}/epoch_results.csv": "  \n" for e in (1, 2)}
    monkeypatch.setattr(R, "AsyncS3Service", _fake_async_s3(files, {"live": 0, "peak": 0}))
    with pytest.raises(HTTPException) as e:
        asyncio.run(R._concatenated_snapshots_response(7, [1, 2], "f.csv"))
    assert e.value.status_code == 404


def test_a_combined_results_file_missing_from_storage_is_404(monkeypatch):
    """A completed job whose combined file is gone (the object store emptied)
    answered 500."""
    def missing():
        raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "gone"}}, "GetObject")
    monkeypatch.setattr(R, "get_s3", lambda: types.SimpleNamespace(
        Object=lambda bucket, key: types.SimpleNamespace(get=missing)))
    with pytest.raises(HTTPException) as e:
        asyncio.run(R._gzip_s3_response("job_id/7/combined_results.csv", "f.csv.gz"))
    assert e.value.status_code == 404


# --------------------------------------------------------------------------- #
# v1: combined CSV pages and epoch list, streamed from S3
# --------------------------------------------------------------------------- #

def _combined_csv(epochs, n):
    rows = ["epoch,n,idBody,m,x,y,z,vx,vy,vz"]
    for e in epochs:
        rows += [f"{e},{i},{n - i},1.0,{e}.5,{i},0,0,0,0" for i in range(n)]   # ids reversed
    return ("\n".join(rows) + "\n").encode()


def _fake_sync_s3(monkeypatch, data, bodies=None):
    def obj(bucket, key):
        body = _Body(data)
        if bodies is not None:
            bodies.append(body)
        return types.SimpleNamespace(get=lambda: {"Body": body})
    monkeypatch.setattr(R, "get_s3", lambda: types.SimpleNamespace(Object=obj))


def test_combined_page_is_parsed_from_a_stream_and_cut_to_the_budget(monkeypatch):
    monkeypatch.setattr(AC, "PAGE_PARTICLE_BUDGET", 30)
    bodies = []
    _fake_sync_s3(monkeypatch, _combined_csv(range(1, 11), 10), bodies)
    snaps, total, limit = asyncio.run(R.parse_combined_file_paginated(
        job_id=7, results_file_key="k", snapshot_interval=1, num_epochs=10, offset=2, limit=5))
    assert (total, limit) == (10, 3)                       # 30 particles / N=10
    assert [s["epoch"] for s in snaps] == [3, 4, 5]
    assert [p[1] for p in snaps[0]["particles"]] == list(range(9, -1, -1))   # sorted by idBody
    assert bodies[0].closed
    # under the budget the requested limit stands
    monkeypatch.setattr(AC, "PAGE_PARTICLE_BUDGET", 1000)
    snaps, _, limit = asyncio.run(R.parse_combined_file_paginated(
        job_id=7, results_file_key="k", snapshot_interval=1, num_epochs=10, offset=2, limit=5))
    assert limit == 5 and [s["epoch"] for s in snaps] == [3, 4, 5, 6, 7]


def test_combined_page_holds_no_more_than_the_page(monkeypatch):
    """Rows of epochs cut from the page are dropped as soon as N is known."""
    monkeypatch.setattr(AC, "PAGE_PARTICLE_BUDGET", 20)
    lines = _combined_csv(range(1, 6), 10).decode().splitlines()
    snaps, limit = R._combined_page_from_rows(iter(lines), [1, 2, 3, 4, 5], 5, 7)
    assert limit == 2 and [s["epoch"] for s in snaps] == [1, 2]
    # a file of one epoch: N is known only at its end
    snaps, limit = R._combined_page_from_rows(iter(_combined_csv([4], 10).decode().splitlines()), [4], 5, 7)
    assert limit == 2 and [s["epoch"] for s in snaps] == [4]


def test_snapshot_list_of_a_combined_file_is_streamed(monkeypatch):
    bodies = []
    _fake_sync_s3(monkeypatch, _combined_csv([2, 4, 6], 3), bodies)
    job = types.SimpleNamespace(job_id=7, status="COMPLETED", results_file_key="job_id/7/combined_results.csv",
                                snapshot_interval=2, current_epoch=6)
    monkeypatch.setattr(R, "Job", types.SimpleNamespace(get=lambda *a: job, job_id=None, DoesNotExist=LookupError))
    monkeypatch.setattr(R, "is_combined_results_file", lambda key: True)
    out = asyncio.run(R.list_snapshots(7, current_user=None))
    assert [s.epoch for s in out.snapshots] == [2, 4, 6] and bodies[0].closed
    assert R._epochs_in_csv(["x,y", "1,2"]) == []          # no epoch column


# --------------------------------------------------------------------------- #
# v2 exports, streamed frame by frame
# --------------------------------------------------------------------------- #

def _v2_history(monkeypatch, snapshots, final_epoch=None):
    downloads = []

    def download(job_id, epoch):
        downloads.append(epoch)
        return BF.bodies_to_binary([{"n": i, "idBody": i, "m": 1.0, "x": float(epoch), "y": 0.0, "z": 0.0,
                                     "vx": 0.0, "vy": 0.0, "vz": 0.0} for i in (1, 2)])
    final = ("epoch,n,idBody,m,x,y,z,vx,vy,vz\r\n" +
             "".join(f"{final_epoch},{i},{i},1.0,{final_epoch},0,0,0,0,0\r\n" for i in (1, 2))) if final_epoch else None

    class S3:
        def get_object(self, Bucket, Key):
            if final is None:
                raise FileNotFoundError(Key)
            return {"Body": _Body(final.encode())}
    patch_where_used(monkeypatch, FS, "list_snapshots", lambda job_id: list(snapshots))
    patch_where_used(monkeypatch, FS, "download_snapshot", download)
    patch_where_used(monkeypatch, FS, "get_s3_client", lambda: S3())
    row = types.SimpleNamespace(job_id=9, job_type=A.JobType.DISTRIBUTED_V2.value, status="COMPLETED")
    monkeypatch.setattr(A, "_v2_job", lambda job_id: row)
    monkeypatch.setattr(A, "Job", types.SimpleNamespace(get_or_none=lambda *a: row, job_id=None))
    return A, downloads, final


def test_v2_full_history_streams_one_frame_per_chunk(monkeypatch):
    A, downloads, _ = _v2_history(monkeypatch, [2, 4, 6], final_epoch=7)
    response = A.download_full_history(9, current_user=None)
    assert downloads == []                                  # nothing read before the stream starts
    chunks = _drain(response)
    assert len(chunks) == 5                                 # header, three frames, the final state
    assert b"".join(chunks).decode() == "".join(A.iter_full_history_csv(9, completed=True))
    assert response.headers["content-disposition"] == "attachment; filename=v2_full_history_9.csv"


def test_v2_full_history_aborts_on_an_unreadable_frame(monkeypatch):
    """Already sent, a frame cannot become a 502: the stream is aborted (the
    client sees a truncated transfer), never completed without the frame."""
    A, downloads, _ = _v2_history(monkeypatch, [2, 4], final_epoch=5)
    good = FS.download_snapshot

    def flaky(job_id, epoch):
        if epoch == 4:
            raise OSError("timeout")
        return good(job_id, epoch)
    patch_where_used(monkeypatch, FS, "download_snapshot", flaky)
    sent = []
    parts = A.download_full_history(9, current_user=None).body_iterator
    with pytest.raises(A.IncompleteHistoryError, match=r"\[4\]"):
        async def consume():
            async for chunk in parts:
                sent.append(chunk)
        asyncio.run(consume())
    assert len(sent) == 2                                   # the header and epoch 2


def test_v2_results_stream_the_final_csv_in_chunks(monkeypatch):
    A, _, final = _v2_history(monkeypatch, [2, 4], final_epoch=5)
    response = A.download_results(9, current_user=None)
    assert b"".join(_drain(response)).decode() == final
    assert response.headers["content-disposition"] == "attachment; filename=v2_results_9.csv"


# --------------------------------------------------------------------------- #
# Animation: page budget and the full-animation cache
# --------------------------------------------------------------------------- #

def test_effective_page_limit():
    assert PAGE_PARTICLE_BUDGET == 200_000                  # the dashboard's AnimationViewer budget
    assert effective_page_limit(100, 1000) == 100
    assert effective_page_limit(100, 10_000) == 20
    assert effective_page_limit(100, 10_000_000) == 1       # at least one snapshot
    assert effective_page_limit(20, 0) == 20


def test_v2_animation_page_is_cut_to_the_budget(monkeypatch):
    monkeypatch.setattr(AC, "PAGE_PARTICLE_BUDGET", 25)
    state = BF.bodies_to_binary([{"n": i, "idBody": i, "m": 1.0, "x": 1.0, "y": 0.0, "z": 0.0,
                                  "vx": 0.0, "vy": 0.0, "vz": 0.0} for i in range(10)])
    downloads = []
    patch_where_used(monkeypatch, FS, "list_snapshots", lambda job_id: list(range(1, 31)))
    patch_where_used(monkeypatch, FS, "download_snapshot", lambda job_id, e: downloads.append(e) or state)
    job = types.SimpleNamespace(job_id=3, status="IN_PROGRESS", snapshot_interval=1, num_epochs=30)
    r = asyncio.run(R.get_v2_animation_data_paginated(job, offset=4, limit=10))
    assert (r.limit, [s.epoch for s in r.snapshots], r.has_next) == (2, [5, 6], True)
    assert sorted(downloads) == [5, 6]                     # the cut snapshots were never read


def test_full_animation_cache_is_bounded_lru(monkeypatch):
    monkeypatch.setattr(AC, "_full_animation_cache", type(AC._full_animation_cache)())
    monkeypatch.setattr(AC, "MAX_FULL_CACHE_MB", 1)

    def anim(n_particles):
        return {"snapshots": [{"particles": [[0.0, 0.0, 0.0, 1.0]] * n_particles}]}
    third = (1024 * 1024 // AC.BYTES_PER_PARTICLE) // 3
    assert not AC.cache_full_animation(1, anim(4 * third))  # alone over the bound
    for job in (1, 2, 3):
        assert AC.cache_full_animation(job, anim(third))
    assert AC.get_cached_full_animation(1) is not None      # 1 is now the most recently used
    assert AC.cache_full_animation(4, anim(third))          # evicts 2, the least recently used
    assert [j for j in (1, 2, 3, 4) if AC.get_cached_full_animation(j)] == [1, 3, 4]
    assert AC.clear_expired_cache() == 0


# --------------------------------------------------------------------------- #
# v1: snapshot existence checks bounded in requests and threads
# --------------------------------------------------------------------------- #

class _CountingS3Client:
    """head_object / list_objects_v2 over a set of keys, counting requests and
    the threads they ran on."""

    def __init__(self, keys):
        self.keys = sorted(keys)
        self.heads = self.lists = 0
        self.threads = set()
        self._current = threading.current_thread

    def head_object(self, Bucket, Key):
        self.heads += 1
        self.threads.add(self._current().name)
        if Key not in self.keys:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")    # as boto3 reports it

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        client = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                matching = [k for k in client.keys if k.startswith(Prefix)]
                for i in range(0, max(len(matching), 1), 1000):
                    client.lists += 1
                    client.threads.add(client._current().name)
                    yield {"Contents": [{"Key": k} for k in matching[i:i + 1000]]}
        return Paginator()


def _fake_counting_s3(monkeypatch, keys):
    client = _CountingS3Client(keys)
    monkeypatch.setattr(R, "get_s3", lambda: types.SimpleNamespace(meta=types.SimpleNamespace(client=client)))
    return client


def test_many_snapshot_epochs_are_verified_with_a_listing_not_a_head_each(monkeypatch):
    """verify_snapshots_exist gathered one HEAD per epoch on the default
    executor: a 50k-epoch job queued 50k requests ahead of /api/ready."""
    epochs = list(range(10, 50_001, 10))                      # 5000 snapshots
    keys = {f"job_id/3/epoch_num/{e}/epoch_results.csv" for e in epochs if e % 70}
    keys |= {f"job_id/3/epoch_num/{e}/metadata.json" for e in epochs}          # not snapshots
    keys |= {"job_id/31/epoch_num/10/epoch_results.csv"}                       # another job
    client = _fake_counting_s3(monkeypatch, keys)
    found = asyncio.run(R.verify_snapshots_exist(epochs, 3))
    assert found == [e for e in epochs if e % 70]             # in order, missing ones dropped
    assert client.heads == 0 and client.lists <= 10           # 10000 keys / 1000 per page
    assert len(client.threads) == 1                           # one worker thread


def test_a_page_of_snapshot_epochs_is_verified_with_heads(monkeypatch):
    keys = {f"job_id/3/epoch_num/{e}/epoch_results.csv" for e in (1, 2, 4)}
    client = _fake_counting_s3(monkeypatch, keys)
    assert asyncio.run(R.verify_snapshots_exist([1, 2, 3, 4], 3)) == [1, 2, 4]
    assert (client.heads, client.lists, len(client.threads)) == (4, 0, 1)


def test_an_outage_while_verifying_snapshots_is_a_503_not_missing_epochs(monkeypatch):
    """Any HEAD error counted as a missing snapshot: during an S3 outage a page
    lost its epochs, and a completed job's page was cached that way."""

    class Down(_CountingS3Client):
        def head_object(self, Bucket, Key):
            raise EndpointConnectionError(endpoint_url="http://minio:9000")
    client = Down(set())
    monkeypatch.setattr(R, "get_s3", lambda: types.SimpleNamespace(meta=types.SimpleNamespace(client=client)))
    with pytest.raises(HTTPException) as err:
        asyncio.run(R.verify_snapshots_exist([1, 2, 3], 3))
    assert err.value.status_code == 503


def test_verifying_snapshots_leaves_the_default_executor_free(monkeypatch):
    """While a long job's snapshots are verified, other default-executor work
    (the /api/ready database check) still gets a thread at once."""
    release = threading.Event()

    class Slow(_CountingS3Client):
        def head_object(self, Bucket, Key):
            release.wait(5)
            return super().head_object(Bucket, Key)
    client = Slow({f"job_id/3/epoch_num/{e}/epoch_results.csv" for e in range(1, 33)})
    monkeypatch.setattr(R, "get_s3", lambda: types.SimpleNamespace(meta=types.SimpleNamespace(client=client)))

    async def main():
        verify = asyncio.create_task(R.verify_snapshots_exist(list(range(1, 33)), 3))
        await asyncio.sleep(0.05)
        start = time.monotonic()
        await asyncio.to_thread(lambda: None)                 # the readiness probe's slot
        waited = time.monotonic() - start
        release.set()
        return waited, await verify
    waited, found = asyncio.run(main())
    assert waited < 1 and found == list(range(1, 33))
