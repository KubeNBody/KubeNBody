# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The start-up every worker entrypoint shares: a GPU check for GPU workers
(fail fast), the buckets it writes to, the metrics and liveness server, the
wait for the API's schema migration, then the worker's loop."""

import sys
from typing import Callable, Optional

import worker_metrics
from job_timestamps import wait_for_job_schema
from logging_config import setup_logging
from workers.backends.gpu_backend import GPU_AVAILABLE, get_gpu_info


def check_gpu_or_exit(logger) -> None:
    """Exit unless a CUDA device is usable (PyCUDA imported, a device found)."""
    if not GPU_AVAILABLE:
        logger.error("=" * 80)
        logger.error("NO GPU DEVICE FOUND (or PyCUDA could not be imported)")
        logger.error("GPU worker requires a CUDA-capable GPU")
        logger.error("=" * 80)
        sys.exit(1)
    gpu_info = get_gpu_info()
    logger.info("=" * 80)
    logger.info("GPU AVAILABLE")
    logger.info(f"Device: {gpu_info['name']}")
    logger.info(f"Compute capability: {gpu_info['compute_capability']}")
    logger.info(f"Total memory: {gpu_info['total_memory_mb']:.0f} MB")
    logger.info("=" * 80)


def run_worker(title: str, log_name: str, worker_type: str, device: str, queue_name: str,
               make_worker: Callable[[], object], prepare: Optional[Callable[[], None]] = None) -> None:
    """Start a worker. worker_type and device label its metrics ('distributed',
    'distributed_v2' or 'monolithic', 'cpu' or 'gpu'). prepare (the buckets)
    runs before the metrics server, make_worker after the schema wait."""
    logger = setup_logging(log_name)
    if device == 'gpu':
        check_gpu_or_exit(logger)
    if prepare is not None:
        prepare()

    logger.info("Starting metrics server on port 9090...")
    worker_metrics.start_metrics_server(worker_type, device)

    # Wait for the API's schema migration (new model columns) before consuming.
    # After the metrics server, so /healthz (liveness probe) answers meanwhile.
    wait_for_job_schema()

    logger.info("=" * 80)
    logger.info(f"{title} starting")
    logger.info("=" * 80)
    make_worker().run(queue_name=queue_name)
