# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
import time
from datetime import datetime, timezone

from peewee import (
    AutoField,
    BooleanField,
    CharField,
    DateTimeField,
    FloatField,
    ForeignKeyField,
    IntegerField,
    InterfaceError,
    Model,
    OperationalError,
    TextField,
)
from playhouse.pool import PooledPostgresqlDatabase
from playhouse.shortcuts import ReconnectMixin

from config import settings
from constants import JOB_NAME_MAX, SOFTENING_SQ
from enums import JobStatus, JobType, PackageStatus
from logging_config import setup_logging


DEFAULT_TIME_STEP = settings.worker.default_time_step

logger = setup_logging('models')

def utcnow():
    """Helper to get current UTC time (replaces deprecated datetime.utcnow())."""
    return datetime.now(timezone.utc)

class ReconnectingDB(ReconnectMixin, PooledPostgresqlDatabase):
    """PooledPostgresqlDatabase that auto-reconnects on stale/dead connections.

    ReconnectMixin catches connection errors (e.g. pgBouncer dropping idle
    connections) and transparently retries with a fresh connection.
    Zero overhead on healthy queries.

    Nothing connects at import: the first query connects (autoconnect), and
    until one connection has succeeded, connect() retries max_retries times,
    retry_delay apart, so a service started before PostgreSQL waits for it.
    """
    reconnect_errors = (
        (OperationalError, 'connection already closed'),
        (InterfaceError, 'connection already closed'),
        (OperationalError, 'server closed the connection unexpectedly'),
        (OperationalError, 'SSL connection has been closed unexpectedly'),
        (OperationalError, 'terminating connection due to administrator command'),
    )

    def __init__(self, *args, max_retries: int, retry_delay: float, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._connected_once = False

    def connect(self, reuse_if_open=False):
        if self._connected_once:
            return super().connect(reuse_if_open)
        cfg = settings.database
        attempts = max(1, self.max_retries)
        for attempt in range(1, attempts + 1):
            try:
                logger.info(f"Attempting to connect to PostgreSQL at {cfg.host}:{cfg.port} "
                            f"(attempt {attempt}/{attempts})")
                opened = super().connect(reuse_if_open)
                self._connected_once = True
                logger.info("Successfully connected to PostgreSQL")
                return opened
            except Exception as e:
                if attempt < attempts:
                    logger.warning(f"Failed to connect to PostgreSQL: {e}. Retrying in {self.retry_delay} seconds...")
                    time.sleep(self.retry_delay)
                else:
                    logger.error(f"Failed to connect to PostgreSQL after {attempts} attempts")
                    # an OperationalError, so the callers' transient-error
                    # checks see an outage
                    raise OperationalError(f"Could not connect to PostgreSQL at {cfg.host}:{cfg.port} "
                                           f"after {attempts} attempts") from e


def make_database() -> ReconnectingDB:
    """The connection pool, configured from settings.database. Not connected."""
    cfg = settings.database
    return ReconnectingDB(
        cfg.name,
        user=cfg.user,
        password=cfg.password,
        host=cfg.host,
        port=cfg.port,
        max_connections=cfg.max_connections,
        stale_timeout=cfg.stale_timeout,
        timeout=cfg.connection_timeout,
        autoconnect=True,
        max_retries=cfg.max_retries,
        retry_delay=cfg.retry_delay,
    )


db = make_database()


class InputFile(Model):
    """Represents an uploaded input file that can be reused across multiple jobs."""
    file_id = AutoField(primary_key=True)
    user_friendly_name = CharField(max_length=255, unique=True, index=True)
    original_filename = CharField(max_length=255)
    s3_key = CharField(unique=True, index=True)
    s3_bucket = CharField(default=settings.s3.input_files_bucket)
    file_size = IntegerField()
    num_bodies = IntegerField()
    created_at = DateTimeField(default=utcnow)
    last_used_at = DateTimeField(default=utcnow)
    usage_count = IntegerField(default=0)
    description = CharField(null=True, max_length=1000)

    class Meta:
        database = db
        table_name = 'input_files'
        indexes = (
            (('user_friendly_name',), True),  # Unique index
            (('s3_key',), True),  # Unique index
            (('created_at',), False),  # For sorting
            (('last_used_at',), False),  # For sorting
        )


class Job(Model):
    job_id = AutoField()
    num_epochs = IntegerField(column_name='num_of_epochs')
    current_epoch = IntegerField(default=0)
    package_size = IntegerField()
    num_packages = IntegerField(column_name='num_of_packages')
    input_file_key = CharField()
    results_file_key = CharField(null=True)
    status = CharField(default=JobStatus.PENDING.value)
    time_step = FloatField(default=DEFAULT_TIME_STEP)
    snapshot_interval = IntegerField(default=5)
    filename = CharField(null=True)
    file_size = IntegerField(null=True)
    job_type = CharField(default=JobType.DISTRIBUTED.value)
    created_at = DateTimeField(default=utcnow)
    updated_at = DateTimeField(default=utcnow)

    # Automatic resume system fields
    retry_count = IntegerField(default=0)
    last_retry_at = DateTimeField(null=True)
    worker_id = CharField(null=True)
    leased_at = DateTimeField(null=True)
    input_bucket = CharField(default=settings.s3.input_files_bucket)

    # GPU acceleration fields
    gpu_enabled = BooleanField(default=False)

    # Algorithm selection fields
    force_algorithm = CharField(default='direct')  # 'direct' or 'barnes_hut'
    barnes_hut_theta = FloatField(default=0.5)  # Opening angle for Barnes-Hut

    # Softening parameter
    softening_sq = FloatField(default=SOFTENING_SQ)  # Plummer softening squared (eps^2)

    # Integration method selection
    integration_method = CharField(default='symplectic_euler')  # an integrators.IntegrationMethod value

    # Adaptive timestepping (softening-length criterion, dt = eta*sqrt(eps/a_max))
    adaptive_enabled = BooleanField(default=False)
    adaptive_eta = FloatField(default=0.02)       # Safety factor for dt estimation
    adaptive_dt_min = FloatField(null=True)        # Minimum allowed timestep (None = use 1e-10)
    adaptive_dt_max = FloatField(null=True)        # Maximum allowed timestep (None = use time_step)
    current_simulation_time = FloatField(default=0.0)  # Actual elapsed simulation time

    # File library reference (nullable for backwards compatibility)
    input_file_id = IntegerField(null=True, index=True)  # Foreign key to InputFile

    # Reproducibility metadata (JSON string of environment info)
    metadata_json = CharField(null=True)

    # User-supplied description / note for this job (free text)
    description = TextField(null=True)
    # User-supplied short title, shown instead of the input file name
    name = CharField(max_length=JOB_NAME_MAX, null=True)

    # Package partitioning strategy (V2 distributed): how bodies are permuted
    # once at job creation so contiguous package slices take a given shape.
    # 'linear' (file order, default), 'random' (seeded shuffle) or 'spatial'
    # (Morton/Z-order locality). partition_seed feeds the 'random' shuffle.
    partition_strategy = CharField(default='linear')
    partition_seed = IntegerField(default=0)

    # When a worker first started computing, and when the job was finalized.
    # Durations = completed_at - started_at exclude queueing.
    started_at = DateTimeField(null=True)
    completed_at = DateTimeField(null=True)

    # How many times the job was resumed after an interrupted run. Unlike
    # retry_count (a retry budget reset on progress) it is never
    # reset, so a recovered run stays identifiable.
    recovery_count = IntegerField(default=0)

    # When the monolithic owner last handed the job back. Cleared when a
    # worker takes the lease. While set, a watchdog republish does not count
    # a retry (the hand-back's message may just be queued).
    handed_back_at = DateTimeField(null=True)

    # v1 distributed: the step the next epoch runs at (an adaptive job's
    # changes every epoch), so a resume continues with it. NULL: time_step.
    next_time_step = FloatField(null=True)

    class Meta:
        database = db
        indexes = (
            # Index for status filtering in job lists
            (('status',), False),

            # Index for lease queries (automatic resume system)
            # NOTE: Migration SQL includes partial index WHERE worker_id IS NOT NULL
            # Peewee Meta.indexes does NOT support partial indexes (WHERE clauses)
            # The actual partial index is created by
            # migrations/005_add_automatic_resume_fields.py:
            #   CREATE INDEX idx_job_lease ON job(worker_id, leased_at) WHERE worker_id IS NOT NULL
            (('worker_id', 'leased_at'), False),

            # Index for watchdog stale job detection
            # NOTE: Migration SQL includes partial index WHERE status = 'IN_PROGRESS'
            # Peewee Meta.indexes does NOT support partial indexes (WHERE clauses)
            # The actual partial index is created by
            # migrations/005_add_automatic_resume_fields.py:
            #   CREATE INDEX idx_job_stale ON job(status, job_type, updated_at) WHERE status = 'IN_PROGRESS'
            (('status', 'job_type', 'updated_at'), False),

            # Index for job_type filtering (distributed, monolithic, distributed_v2)
            (('job_type',), False),

            # Index for algorithm filtering
            (('force_algorithm',), False),
        )


class Package(Model):
    package_id = AutoField()
    package_number = IntegerField() # Number in a specific batch
    epoch_num = IntegerField()
    job = ForeignKeyField(Job, backref='packages')
    file_key = CharField(null=True)
    status = CharField(default=PackageStatus.PENDING.value)

    class Meta:
        database = db
        indexes = (
            # Composite index for progress calculation queries
            (('job', 'epoch_num', 'status'), False),
            # Index for status-based queries
            (('status',), False),
            # Index for job-specific queries
            (('job', 'status'), False),
        )


class ComparisonJob(Model):
    """Persistent comparison job model."""
    comparison_id = AutoField()
    reference_job_id = IntegerField(index=True)
    comparison_job_ids = CharField()          # JSON-encoded list: "[1, 2, 3]"
    max_snapshots = IntegerField(null=True, default=100)  # NULL = all snapshots (column is nullable)
    status = CharField(default='PENDING')     # PENDING, COMPUTING, COMPLETED, COMPLETED_EMPTY, FAILED
    error_message = CharField(null=True, max_length=500)
    created_at = DateTimeField(default=utcnow)
    updated_at = DateTimeField(default=utcnow)
    started_at = DateTimeField(null=True)
    computation_time_ms = IntegerField(null=True)
    results_file_key = CharField(null=True)
    jobs_compared = IntegerField(default=0)
    jobs_skipped = IntegerField(default=0)
    job_fingerprints = CharField(null=True)   # JSON: settings snapshot for staleness detection

    class Meta:
        database = db
        indexes = (
            (('status',), False),
            (('reference_job_id',), False),
            (('created_at',), False),
        )


class User(Model):
    """User model for authentication."""
    user_id = AutoField()
    username = CharField(unique=True, max_length=100)
    hashed_password = CharField(max_length=255)
    is_active = BooleanField(default=True)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        database = db
        indexes = (
            # Index for username lookups during authentication
            (('username',), True),  # Unique index
        )

