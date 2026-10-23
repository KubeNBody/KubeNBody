# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Loads simulation snapshots from S3 and converts them to NumPy arrays
for metric computation. Supports both V1 (CSV) and V2 (binary) formats.

Includes streaming/batch support for memory-efficient processing of large simulations.
"""

import asyncio
import contextlib
import io
import os
from dataclasses import dataclass, field
from typing import AsyncIterator, Collection, List, Optional, Tuple

import numpy as np
import pandas as pd

from async_file_service import EPOCH_RESULTS_BUCKET_NAME, AsyncS3Service
from logging_config import setup_logging
from models import Job
from schemas import JobStatus, JobType
from transient_errors import is_missing_object
from utils.file_parsing import BODY_COLUMNS
from utils.job_helpers import is_combined_results_file, snapshot_epochs
from utils.snapshot_ordering import canonicalize_by_id
from v2.binary_format import binary_to_numpy
from v2.file_service_v2 import download_snapshot, list_snapshots


logger = setup_logging('utils.snapshot_loader')


# Type alias for snapshot data
SnapshotData = Tuple[np.ndarray, np.ndarray, np.ndarray]  # (positions, velocities, masses)

# Streaming batch size
DEFAULT_BATCH_SIZE = 10  # Process 10 snapshots at a time


@dataclass
class SnapshotReport:
    """Snapshots a stream should have yielded but could not find. Only a
    missing object is skipped. Any other read error ends the stream, so a
    transient S3 failure cannot shorten a result that is then cached."""
    missing: List[int] = field(default_factory=list)


def select_snapshot_epochs(epochs: List[int], max_snapshots: Optional[int]) -> List[int]:
    """At most max_snapshots of the (sorted) epochs: every k-th, counted back
    from the last, for the smallest k that fits. The first N alone ended each
    job's series at a different simulation time when the jobs' steps differ.
    A fixed stride ending at the last snapshot (rather than evenly spaced
    picks) keeps two jobs whose snapshot spacings are commensurate on common
    times, which the comparison then uses directly (TrajectoryPairing)."""
    if not max_snapshots or len(epochs) <= max_snapshots:
        return list(epochs)
    stride = -(-len(epochs) // max_snapshots)
    return list(epochs[len(epochs) - 1::-stride][::-1])


async def stream_job_snapshots(
    job: Job,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_snapshots: Optional[int] = None,
    report: Optional[SnapshotReport] = None,
    epochs: Optional[Collection[int]] = None,
) -> AsyncIterator[Tuple[int, SnapshotData]]:
    """Yield (epoch, (positions, velocities, masses)) for the job's snapshots one
    at a time, in epoch order, so a large job is never held in memory whole.

    batch_size snapshots are fetched in parallel. max_snapshots: every k-th
    back from the last (select_snapshot_epochs), or epochs: exactly these
    planned snapshots (a comparison job's at the reference job's times).
    report: filled with the epochs that were expected but not found (for a job
    that is still running, only gaps before its latest snapshot count)."""
    report = report if report is not None else SnapshotReport()
    last = 0
    planned = _get_snapshot_epochs(job.num_epochs, job.snapshot_interval)
    if epochs is not None:
        chosen = set(epochs)
        wanted = [e for e in planned if e in chosen]
    else:
        wanted = select_snapshot_epochs(planned, max_snapshots)
    # without a selection every epoch a combined file holds is used
    subset = epochs is not None or bool(max_snapshots)
    if job.job_type == JobType.DISTRIBUTED_V2.value:
        stream = _stream_v2_snapshots(job, batch_size, wanted, report)
    else:
        stream = _stream_v1_snapshots(job, batch_size, wanted, subset, report)
    # closed with this generator, so a consumer that stops early (a
    # comparison past the other job's last snapshot) removes the temp file now
    async with contextlib.aclosing(stream):
        async for item in stream:
            last = item[0]
            yield item
    if job.status != JobStatus.COMPLETED.value:
        report.missing = [e for e in report.missing if e < last]
    if report.missing:
        logger.warning(f"Job {job.job_id}: {len(report.missing)} snapshots not found "
                       f"(first epochs: {sorted(report.missing)[:10]})")


async def _stream_v1_snapshots(
    job: Job,
    batch_size: int,
    snapshot_epochs: List[int],
    subset: bool,
    report: SnapshotReport,
) -> AsyncIterator[Tuple[int, SnapshotData]]:
    """Stream V1 CSV snapshots: snapshot_epochs, or from a combined file
    every epoch it holds unless subset."""
    service = AsyncS3Service()

    # For combined files, we need to download once and parse streaming
    if job.results_file_key and is_combined_results_file(job.results_file_key):
        logger.info(f"Streaming snapshots from combined CSV for job {job.job_id}")
        wanted = set(snapshot_epochs) if subset else None
        found = set()
        async for item in _stream_combined_csv(service, job.results_file_key, wanted):
            found.add(item[0])
            yield item
        report.missing.extend(e for e in snapshot_epochs if e not in found)
        return

    # For individual files, batch the downloads
    logger.info(f"Streaming {len(snapshot_epochs)} individual CSV snapshots for job {job.job_id}")

    for batch_start in range(0, len(snapshot_epochs), batch_size):
        batch_epochs = snapshot_epochs[batch_start:batch_start + batch_size]

        # Download batch in parallel
        tasks = []
        for epoch in batch_epochs:
            file_key = f'job_id/{job.job_id}/epoch_num/{epoch}/epoch_results.csv'
            tasks.append(_download_and_parse_csv(service, file_key, epoch))

        async with service:                 # one client per batch
            results = await asyncio.gather(*tasks, return_exceptions=True)

        # Yield results from batch
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                if not is_missing_object(result):
                    raise result
                report.missing.append(batch_epochs[i])
                continue
            yield result


async def _download_and_parse_csv(
    service: AsyncS3Service,
    file_key: str,
    epoch: int
) -> Tuple[int, SnapshotData]:
    """Download and parse a single CSV snapshot."""
    raw_csv = await service.download_file(EPOCH_RESULTS_BUCKET_NAME, file_key)
    pos, vel, mass = _parse_csv_to_numpy(raw_csv)
    return epoch, (pos, vel, mass)


async def _stream_combined_csv(
    service: AsyncS3Service,
    file_key: str,
    epochs: Optional[Collection[int]] = None,
) -> AsyncIterator[Tuple[int, SnapshotData]]:
    """
    Stream epochs from a combined CSV file.

    Streams the S3 object to a temp file on disk first so the entire CSV is
    never held in memory at once (a 2000-snapshot combined file can easily
    be 200 MiB+).

    Parsed with pandas' C engine in bounded chunks, each in asyncio.to_thread,
    so the event loop and the /api/health liveness probe stay responsive. `epochs`: only these (None: all).
    """
    path = await service.stream_to_tempfile(EPOCH_RESULTS_BUCKET_NAME, file_key)
    try:
        gen = iter_epochs_pandas(path, epochs=epochs)
        while True:
            # next() pulls one epoch's worth of parsing onto a worker
            # thread. The loop stays free between epochs.
            item = await asyncio.to_thread(next, gen, None)
            if item is None:
                return
            yield item
    finally:
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError as e:
                logger.warning(f"Failed to remove temp file {path}: {e}")


def iter_epochs_pandas(path: str, chunksize: int = 500_000, epochs: Optional[Collection[int]] = None):
    """Yield (epoch, (positions, velocities, masses)) from a combined CSV:
    every epoch, or (`epochs`) only those.

    Reads in bounded chunks (memory stays ~chunk-sized regardless of file
    size). Epochs are written contiguously, so only the chunk's last epoch
    can spill into the next chunk — it is carried over and completed there.
    """

    header_cols = set(pd.read_csv(path, nrows=0).columns)
    mass_col = 'm' if 'm' in header_cols else ('mass' if 'mass' in header_cols else None)
    # Keep the idBody column so _frame_to_numpy can canonicalise body order for
    # cross-job comparison (see utils.snapshot_ordering).
    id_col = 'idBody' if 'idBody' in header_cols else ('idbody' if 'idbody' in header_cols else None)
    usecols = [c for c in ['epoch', id_col, mass_col, 'x', 'y', 'z', 'vx', 'vy', 'vz']
               if c is not None and c in header_cols]

    carry = None
    # float_precision='high': C-speed parsing within ~1e-12 relative of
    # Python's float() — immaterial for conservation metrics/animations
    # ('round_trip' would be bit-exact but ~3x slower, this loader never
    # feeds simulation resume, which has its own parser).
    for chunk in pd.read_csv(path, usecols=usecols, chunksize=chunksize,
                             float_precision='high'):
        if carry is not None:
            chunk = pd.concat([carry, chunk], ignore_index=True)
            carry = None

        chunk['epoch'] = pd.to_numeric(chunk['epoch'], errors='coerce')
        chunk = chunk.dropna(subset=['epoch'])
        chunk = chunk[chunk['epoch'] > 0]
        if chunk.empty:
            continue

        # The last epoch in the chunk may continue in the next chunk
        last_epoch = chunk['epoch'].iloc[-1]
        carry = chunk[chunk['epoch'] == last_epoch]
        complete = chunk[chunk['epoch'] != last_epoch]

        for epoch, group in complete.groupby('epoch', sort=True):
            if epochs is not None and int(epoch) not in epochs:
                continue
            yield int(epoch), _frame_to_numpy(group, mass_col)

    if carry is not None and not carry.empty:
        if epochs is not None and int(carry['epoch'].iloc[0]) not in epochs:
            return
        yield int(carry['epoch'].iloc[0]), _frame_to_numpy(carry, mass_col)


def _frame_to_numpy(df, mass_col: Optional[str]) -> SnapshotData:
    """Convert a per-epoch DataFrame slice to (positions, velocities, masses)."""
    positions = df[['x', 'y', 'z']].to_numpy(dtype=np.float64)
    velocities = df[['vx', 'vy', 'vz']].to_numpy(dtype=np.float64)
    if mass_col is not None:
        masses = df[mass_col].to_numpy(dtype=np.float64)
    else:
        masses = np.ones(len(df), dtype=np.float64)
    id_col = 'idBody' if 'idBody' in df.columns else ('idbody' if 'idbody' in df.columns else None)
    ids = df[id_col].to_numpy() if id_col is not None else None
    return canonicalize_by_id(positions, velocities, masses, ids)


async def _stream_v2_snapshots(
    job: Job,
    batch_size: int,
    wanted: List[int],
    report: SnapshotReport,
) -> AsyncIterator[Tuple[int, SnapshotData]]:
    """Stream V2 binary snapshots: those of the wanted (planned) epochs."""
    loop = asyncio.get_running_loop()
    listed = set(await loop.run_in_executor(None, list_snapshots, job.job_id))
    # Selected from the planned snapshots, like V1, not from those listed: a
    # lost snapshot then counts as missing instead of shifting every pick
    # (the worker writes every planned snapshot, the final one always)
    available_epochs = [e for e in wanted if e in listed]
    report.missing.extend(e for e in wanted if e not in listed)

    logger.info(f"Streaming {len(available_epochs)} V2 snapshots for job {job.job_id}")

    def load_single(epoch: int) -> Tuple[int, SnapshotData]:
        binary_data = download_snapshot(job.job_id, epoch)
        positions, velocities, masses, ids = binary_to_numpy(binary_data, validate=False)
        # Canonicalise by idBody so 'random'/'spatial' partitioned jobs compare
        # against file-order jobs body-for-body (no-op for 'linear'/monolithic).
        return epoch, canonicalize_by_id(positions, velocities, masses, ids)

    for batch_start in range(0, len(available_epochs), batch_size):
        batch_epochs = available_epochs[batch_start:batch_start + batch_size]

        tasks = [loop.run_in_executor(None, load_single, epoch) for epoch in batch_epochs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                if not is_missing_object(result):
                    raise result
                report.missing.append(batch_epochs[i])
                continue
            yield result


def read_body_frame(raw_csv: str) -> pd.DataFrame:
    """A body CSV as the API validates it (utils.file_parsing): spaces after
    the delimiters and around the header names, and the case of the names,
    are ignored, so an input stored before uploads were normalised
    ("n, idBody, ...", "IDBODY") reads with the canonical columns."""
    df = pd.read_csv(io.StringIO(raw_csv), float_precision='high', skipinitialspace=True)
    canonical = {c.lower(): c for c in BODY_COLUMNS}
    df.columns = [canonical.get(str(c).strip().lower(), str(c).strip()) for c in df.columns]
    return df


def _parse_csv_to_numpy(raw_csv: str) -> SnapshotData:
    """(positions, velocities, masses) of one CSV snapshot (or input file)."""
    df = read_body_frame(raw_csv)
    mass_col = 'm' if 'm' in df.columns else ('mass' if 'mass' in df.columns else None)
    return _frame_to_numpy(df, mass_col)


def _get_snapshot_epochs(num_epochs: int, snapshot_interval: int) -> List[int]:
    """
    Calculate which epochs should have snapshots.

    Includes epochs at snapshot_interval multiples plus the final epoch.
    """
    return snapshot_epochs(num_epochs, snapshot_interval)
