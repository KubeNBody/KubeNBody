# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Health, metrics and readiness endpoints. Public (no authentication), for
the Kubernetes probes and monitoring systems."""

import asyncio
import threading
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from peewee import fn
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR

import prometheus_metrics
from api_schemas import HealthResponse, MetricsResponse
from db_threads import ClosingRoute, closes_connection
from file_service import get_s3
from logging_config import setup_logging
from models import Job, Package, db
from rabbitmq import queue_depths, rabbitmq_ping
from schemas import JobStatus, PackageStatus


logger = setup_logging('api.monitoring')

router = APIRouter(tags=["Monitoring"], route_class=ClosingRoute)


QUEUE_DEPTHS_TTL_S = 10
HEALTH_TTL_S = 5
_services = (float('-inf'), {})     # (checked at, services): each check opens connections
_services_lock = threading.Lock()
_queue_depths = (float('-inf'), {})          # (read at, depths): the endpoint is public
_queue_depths_lock = threading.Lock()


def _cached_queue_depths() -> dict:
    """RabbitMQ queue depths, read at most every QUEUE_DEPTHS_TTL_S (each
    read opens a broker connection). {} if the broker cannot be reached."""
    global _queue_depths
    with _queue_depths_lock:
        read_at, depths = _queue_depths
        if time.monotonic() - read_at < QUEUE_DEPTHS_TTL_S:
            return depths
        try:
            depths = queue_depths(timeout_s=3)
        except Exception as e:  # noqa: BLE001 — the job counts are still worth returning
            logger.warning(f"Could not read RabbitMQ queue depths: {e}")
            depths = {}
        _queue_depths = (time.monotonic(), depths)
        return depths


def _count_by_status(model) -> dict:
    """{status: row count} of a table, in one grouped query."""
    query = model.select(model.status, fn.COUNT(model.status).alias('n')).group_by(model.status)
    return {row.status: row.n for row in query}


@router.get("/api/live")
async def liveness_check():
    """Kubernetes liveness probe: the process serves requests. Checks no
    dependency, since restarting the API would not bring one back."""
    return {"status": "alive"}


@router.get("/api/health", response_model=HealthResponse)
async def health_check():
    """
    Service health check endpoint: the state of the database, the broker and
    the object store (each checked once, briefly, in a worker thread so the
    event loop keeps serving while a dependency hangs). The probes use
    /api/live and /api/ready.
    """
    services = await asyncio.to_thread(closes_connection(_cached_services))
    # equality, not `'healthy' in status`: that is true of 'unhealthy: ...' too.
    overall_status = 'healthy' if all(s == 'healthy' for s in services.values()) else 'unhealthy'
    return HealthResponse(
        status=overall_status,
        services=services,
        timestamp=datetime.now()
    )


def _cached_services() -> dict:
    """_check_services, run at most every HEALTH_TTL_S: the endpoint is public,
    and each check opens a database, broker and object store connection."""
    global _services
    with _services_lock:
        checked_at, services = _services
        if time.monotonic() - checked_at >= HEALTH_TTL_S:
            services = _check_services()
            _services = (time.monotonic(), services)
        return services


def _check_services() -> dict:
    """{service: 'healthy' | 'unhealthy'}. The reason is logged, not returned
    (the endpoint is public, and exception texts carry hosts and addresses)."""
    checks = {
        'database': lambda: Job.select().count(),
        'rabbitmq': rabbitmq_ping,                       # one short connection attempt
        's3': lambda: list(get_s3().buckets.all()),
    }
    services = {}
    for name, check in checks.items():
        try:
            check()
            services[name] = 'healthy'
        except Exception as e:
            logger.warning(f"Health check: {name} is unhealthy: {e}")
            services[name] = 'unhealthy'
    return services


@router.get("/api/metrics", response_model=MetricsResponse)
def get_metrics():
    """System metrics."""
    try:
        jobs = _count_by_status(Job)
        active_jobs = jobs.get(JobStatus.IN_PROGRESS.value, 0)
        completed_jobs = jobs.get(JobStatus.COMPLETED.value, 0)
        failed_jobs = jobs.get(JobStatus.FAILED.value, 0)

        total_packages = Package.select().count()

        queue_sizes = _cached_queue_depths()

        return MetricsResponse(
            active_jobs=active_jobs,
            completed_jobs=completed_jobs,
            failed_jobs=failed_jobs,
            total_packages_processed=total_packages,
            queue_sizes=queue_sizes,
            timestamp=datetime.now()
        )

    except Exception as e:
        logger.error(f"Failed to get metrics: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get metrics")


@router.get("/metrics")
def prometheus_metrics_endpoint():
    """Metrics in the Prometheus exposition format."""
    try:
        # Update gauge metrics with current values from database (one
        # grouped query per table, not a COUNT per status on every scrape)
        jobs = _count_by_status(Job)
        job_counts = {s.value.lower(): jobs.get(s.value, 0)
                      for s in (JobStatus.PENDING, JobStatus.IN_PROGRESS, JobStatus.PAUSED,
                                JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)}

        for status_name, count in job_counts.items():
            prometheus_metrics.JOBS_TOTAL.labels(status=status_name).set(count)

        prometheus_metrics.JOBS_ACTIVE.set(job_counts['in_progress'])

        packages = _count_by_status(Package)
        package_counts = {s.value.lower(): packages.get(s.value, 0)
                          for s in (PackageStatus.PENDING, PackageStatus.IN_PROGRESS, PackageStatus.TO_MERGE,
                                    PackageStatus.MERGED, PackageStatus.FAILED)}

        for status_name, count in package_counts.items():
            prometheus_metrics.PACKAGES_TOTAL.labels(status=status_name).set(count)

        return Response(
            content=prometheus_metrics.get_metrics(),
            media_type=prometheus_metrics.get_content_type()
        )

    except Exception as e:
        logger.error(f"Failed to generate Prometheus metrics: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to generate metrics")


@router.get("/api/ready")
async def readiness_check():
    """Kubernetes readiness probe."""
    try:
        await asyncio.to_thread(closes_connection(db.execute_sql), "SELECT 1")
        return {"status": "ready"}
    except Exception:
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Service not ready")
