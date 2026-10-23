# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Prometheus metrics of the KubeNBody API.

Exposes application metrics in Prometheus format for monitoring.
"""
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Info, generate_latest


# Application Info
APP_INFO = Info('nbody', 'KubeNBody application information')
APP_INFO.info({'version': '1.0.0', 'service': 'dashboard-backend'})

# Job Metrics
JOBS_TOTAL = Gauge(
    'nbody_jobs_total',
    'Total number of jobs by status',
    ['status']
)

JOBS_ACTIVE = Gauge(
    'nbody_jobs_active',
    'Number of currently active (in-progress) jobs'
)

JOBS_CREATED = Counter(
    'nbody_jobs_created_total',
    'Total number of jobs created'
)

# Package Metrics
PACKAGES_TOTAL = Gauge(
    'nbody_packages_total',
    'Total number of packages by status',
    ['status']
)

# Simulation Metrics
BODIES_SIMULATED = Counter(
    'nbody_bodies_simulated_total',
    'Total number of bodies simulated across all jobs'
)

EPOCHS_COMPLETED = Counter(
    'nbody_epochs_completed_total',
    'Total number of epochs completed across all jobs'
)


# Helper Functions
def get_metrics():
    """Generate Prometheus metrics output."""
    return generate_latest(REGISTRY)


def get_content_type():
    """Get the content type for Prometheus metrics."""
    return CONTENT_TYPE_LATEST


def record_job_created():
    """Increment the jobs created counter."""
    JOBS_CREATED.inc()


def record_job_completed(num_bodies: int, num_epochs: int):
    """Record a completed job of num_bodies bodies and num_epochs epochs."""
    BODIES_SIMULATED.inc(num_bodies)
    EPOCHS_COMPLETED.inc(num_epochs)
