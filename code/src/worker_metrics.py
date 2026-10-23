# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Prometheus metrics of the KubeNBody workers.

Provides metrics collection and a simple HTTP server for Prometheus scraping.
Workers can start the metrics server and track processing times.
"""
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, Info, generate_latest


# Configuration
try:
    from config import settings as _cfg
    METRICS_PORT = _cfg.api.metrics_port
except Exception:
    METRICS_PORT = int(os.getenv('METRICS_PORT', '9090'))

# Worker Info
WORKER_INFO = Info('nbody_worker', 'N-Body worker information')

# Package Processing Metrics
PACKAGE_PROCESSING_SECONDS = Histogram(
    'nbody_package_processing_seconds',
    'Time spent processing a single package',
    ['worker_type', 'compute_type', 'algorithm'],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0]
)

PACKAGE_PHYSICS_SECONDS = Histogram(
    'nbody_package_physics_seconds',
    'Time spent on physics calculations only (excluding I/O)',
    ['worker_type', 'compute_type', 'algorithm'],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 25.0, 50.0, 100.0]
)

PACKAGES_PROCESSED = Counter(
    'nbody_worker_packages_processed_total',
    'Total number of packages processed by this worker',
    ['worker_type', 'compute_type', 'algorithm', 'status']
)

BODIES_PER_PACKAGE = Histogram(
    'nbody_bodies_per_package',
    'Number of bodies in processed packages',
    ['worker_type'],
    buckets=[100, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000]
)

# I/O Breakdown Metrics (for distributed workers)
PACKAGE_DOWNLOAD_SECONDS = Histogram(
    'nbody_package_download_seconds',
    'Time spent downloading input file from object storage',
    ['worker_type', 'compute_type'],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 25.0]
)

PACKAGE_UPLOAD_SECONDS = Histogram(
    'nbody_package_upload_seconds',
    'Time spent uploading results to object storage',
    ['worker_type', 'compute_type'],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 25.0]
)

PACKAGE_DB_SECONDS = Histogram(
    'nbody_package_db_seconds',
    'Time spent on database operations',
    ['worker_type', 'compute_type'],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0]
)

# Epoch Metrics (for monolithic workers)
EPOCH_BATCH_SECONDS = Histogram(
    'nbody_epoch_batch_seconds',
    'Time spent processing a batch of epochs (between two progress updates)',
    ['compute_type', 'algorithm'],
    buckets=[1.0, 2.5, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0]
)

EPOCH_RATE = Gauge(
    'nbody_epoch_rate',
    'Current epoch processing rate (epochs/second)',
    ['compute_type', 'algorithm']
)

MONOLITHIC_JOB_SECONDS = Histogram(
    'nbody_monolithic_job_seconds',
    'Total time for monolithic job completion',
    ['compute_type', 'algorithm'],
    buckets=[60, 300, 600, 1800, 3600, 7200, 14400, 28800]  # 1min to 8hr
)

EPOCHS_PROCESSED = Counter(
    'nbody_worker_epochs_processed_total',
    'Total number of epochs processed by this worker',
    ['compute_type', 'algorithm']
)

# Worker Status
WORKER_BUSY = Gauge(
    'nbody_worker_busy',
    'Whether the worker is currently processing (1) or idle (0)',
    ['worker_type', 'compute_type']
)

CURRENT_JOB_ID = Gauge(
    'nbody_worker_current_job_id',
    'ID of the job currently being processed (0 if idle)',
    ['worker_type']
)

def observe_package_processing(worker_type: str, compute_type: str, algorithm: str,
                                duration: float, physics_duration: float = None,
                                num_bodies: int = None, success: bool = True):
    """Record a processed package: its duration (and physics_duration) in
    seconds, its size and whether it succeeded. worker_type: 'distributed' or
    'monolithic', compute_type: 'cpu' or 'gpu', algorithm: 'direct' or
    'barnes_hut', as in every function here."""
    PACKAGE_PROCESSING_SECONDS.labels(
        worker_type=worker_type,
        compute_type=compute_type,
        algorithm=algorithm
    ).observe(duration)

    if physics_duration is not None:
        PACKAGE_PHYSICS_SECONDS.labels(
            worker_type=worker_type,
            compute_type=compute_type,
            algorithm=algorithm
        ).observe(physics_duration)

    PACKAGES_PROCESSED.labels(
        worker_type=worker_type,
        compute_type=compute_type,
        algorithm=algorithm,
        status='success' if success else 'failure'
    ).inc()

    if num_bodies is not None:
        BODIES_PER_PACKAGE.labels(worker_type=worker_type).observe(num_bodies)


def observe_io_breakdown(worker_type: str, compute_type: str,
                         download_time: float = None, upload_time: float = None,
                         db_time: float = None):
    """Record a package's I/O times in seconds: input download, results upload
    and database."""
    if download_time is not None and download_time > 0:
        PACKAGE_DOWNLOAD_SECONDS.labels(
            worker_type=worker_type,
            compute_type=compute_type
        ).observe(download_time)

    if upload_time is not None and upload_time > 0:
        PACKAGE_UPLOAD_SECONDS.labels(
            worker_type=worker_type,
            compute_type=compute_type
        ).observe(upload_time)

    if db_time is not None and db_time > 0:
        PACKAGE_DB_SECONDS.labels(
            worker_type=worker_type,
            compute_type=compute_type
        ).observe(db_time)


def observe_epoch_batch(compute_type: str, algorithm: str,
                        batch_time: float, epoch_count: int = 100):
    """Record a batch of monolithic epochs: epoch_count epochs in batch_time
    seconds."""
    EPOCH_BATCH_SECONDS.labels(
        compute_type=compute_type,
        algorithm=algorithm
    ).observe(batch_time)

    # Calculate and set epoch rate
    if batch_time > 0:
        epoch_rate = epoch_count / batch_time
        EPOCH_RATE.labels(
            compute_type=compute_type,
            algorithm=algorithm
        ).set(epoch_rate)

    # Increment total epochs processed
    EPOCHS_PROCESSED.labels(
        compute_type=compute_type,
        algorithm=algorithm
    ).inc(epoch_count)


def observe_monolithic_job(compute_type: str, algorithm: str, total_time: float):
    """Record a completed monolithic job's total time in seconds."""
    MONOLITHIC_JOB_SECONDS.labels(
        compute_type=compute_type,
        algorithm=algorithm
    ).observe(total_time)


def set_worker_status(worker_type: str, compute_type: str, busy: bool, job_id: int = 0):
    """Set the worker's busy status and current job (0 when idle)."""
    WORKER_BUSY.labels(worker_type=worker_type, compute_type=compute_type).set(1 if busy else 0)
    CURRENT_JOB_ID.labels(worker_type=worker_type).set(job_id)


# Metrics HTTP Server
class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler for Prometheus metrics endpoint."""

    def do_GET(self):
        if self.path == '/metrics':
            self.send_response(200)
            self.send_header('Content-Type', CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(generate_latest(REGISTRY))
        elif self.path == '/health' or self.path == '/healthz':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


_metrics_server: HTTPServer = None


def start_metrics_server(worker_type: str, compute_type: str, port: int = None):
    """Start the metrics HTTP server on a background thread and return it. The
    port defaults to METRICS_PORT (or 9090). Each Kubernetes pod has its own
    network namespace, so several workers run locally need different
    METRICS_PORT values."""
    global _metrics_server

    if port is None:
        port = METRICS_PORT

    WORKER_INFO.info({
        'worker_type': worker_type,
        'compute_type': compute_type,
    })

    set_worker_status(worker_type, compute_type, busy=False)

    _metrics_server = HTTPServer(('0.0.0.0', port), MetricsHandler)

    thread = threading.Thread(target=_metrics_server.serve_forever, daemon=True)
    thread.start()

    return _metrics_server
