# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Centralized configuration of KubeNBody.

All environment variable handling is consolidated here using Pydantic settings.
Import settings from this module rather than using os.getenv directly.

Usage:
    from config import settings

    # Access configuration
    db_host = settings.database.host
    redis_host = settings.redis.host
"""

from typing import List, Optional, Tuple

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Shared SettingsConfigDict for all sub-settings: accept either field name
# or the env-variable alias, and ignore unknown env vars instead of failing.
_BASE_CONFIG = SettingsConfigDict(populate_by_name=True, extra='ignore')


class DatabaseSettings(BaseSettings):
    """PostgreSQL database configuration."""

    model_config = _BASE_CONFIG

    name: str = Field(default='nbody', alias='POSTGRES_DB')
    user: str = Field(default='user', alias='POSTGRES_USER')
    password: str = Field(default='password', alias='POSTGRES_PASSWORD')
    host: str = Field(default='localhost', alias='POSTGRES_HOST')
    port: int = Field(default=15432, alias='POSTGRES_PORT')

    # Connection pool settings
    max_connections: int = Field(default=20, alias='DB_MAX_CONNECTIONS')
    stale_timeout: int = Field(default=300, alias='DB_STALE_TIMEOUT')
    connection_timeout: int = Field(default=30, alias='DB_CONNECTION_TIMEOUT')

    # Retry settings
    max_retries: int = Field(default=30, alias='DB_MAX_RETRIES')
    retry_delay: float = Field(default=2.0, alias='DB_RETRY_DELAY')


class RabbitMQSettings(BaseSettings):
    """RabbitMQ message queue configuration."""

    model_config = _BASE_CONFIG

    host: str = Field(default='localhost', alias='RABBITMQ_HOST')
    user: str = Field(default='user', alias='RABBITMQ_USER')
    password: str = Field(default='password', alias='RABBITMQ_PASSWORD')
    port: int = Field(default=5672, alias='RABBITMQ_PORT')

    # Connection settings
    connection_attempts: int = Field(default=10, alias='RABBITMQ_CONNECTION_ATTEMPTS')
    retry_delay: float = Field(default=3.0, alias='RABBITMQ_RETRY_DELAY')
    heartbeat: int = Field(default=60, alias='RABBITMQ_HEARTBEAT')
    blocked_connection_timeout: int = Field(default=300, alias='RABBITMQ_BLOCKED_TIMEOUT')

    # Retry settings
    max_retries: int = Field(default=30, alias='RABBITMQ_MAX_RETRIES')


class RedisSettings(BaseSettings):
    """Redis configuration for V2 architecture."""

    model_config = _BASE_CONFIG

    host: str = Field(default='localhost', alias='REDIS_HOST')
    port: int = Field(default=6379, alias='REDIS_PORT')
    password: Optional[str] = Field(default=None, alias='REDIS_PASSWORD')
    db: int = Field(default=0, alias='REDIS_DB')

    # Sentinel HA configuration
    sentinel_enabled: bool = Field(default=False, alias='REDIS_SENTINEL_ENABLED')
    sentinel_hosts: str = Field(default='redis-ha-sentinel:26379', alias='REDIS_SENTINEL_HOSTS')
    sentinel_master: str = Field(default='nbody-master', alias='REDIS_SENTINEL_MASTER')

    # Connection settings
    socket_timeout: int = Field(default=30, alias='REDIS_SOCKET_TIMEOUT')
    socket_connect_timeout: int = Field(default=10, alias='REDIS_CONNECT_TIMEOUT')

    # Retry settings
    max_retries: int = Field(default=30, alias='REDIS_MAX_RETRIES')
    retry_delay: float = Field(default=2.0, alias='REDIS_RETRY_DELAY')

    # Failover settings (for Sentinel)
    failover_max_retries: int = Field(default=10, alias='REDIS_FAILOVER_MAX_RETRIES')
    failover_base_delay: float = Field(default=0.5, alias='REDIS_FAILOVER_BASE_DELAY')

    # TTL settings
    default_job_ttl_seconds: int = Field(default=86400, alias='REDIS_JOB_TTL')
    # A pending task idle this long is a dead worker's and is reclaimed. A
    # live worker touches its task every third of it (RenewLoop), so 45 s
    # leaves two missed touches of margin. A false reclaim costs a duplicate
    # computation, never a wrong result (registration ignores duplicates).
    stale_task_ms: int = Field(default=45000, alias='REDIS_STALE_TASK_MS')
    # V2 workers read the epoch state from a Sentinel replica for odd-numbered
    # packages (redis_service.get_task_inputs_from_replica): halves the
    # primary's broadcast egress. Off here; the base manifests turn it on.
    replica_reads: bool = Field(default=False, alias='NBODY_V2_REPLICA_READS')

    def get_sentinel_hosts(self) -> List[Tuple[str, int]]:
        """Parse sentinel hosts from comma-separated string."""
        hosts = []
        for host_port in self.sentinel_hosts.split(','):
            host_port = host_port.strip()
            if ':' in host_port:
                host, port = host_port.rsplit(':', 1)
                hosts.append((host, int(port)))
            else:
                hosts.append((host_port, 26379))
        return hosts


class S3Settings(BaseSettings):
    """S3/MinIO object storage configuration."""

    model_config = _BASE_CONFIG

    endpoint_url: str = Field(default='http://localhost:19000', alias='MINIO_URL')
    access_key: str = Field(default='user', alias='MINIO_USER')
    secret_key: str = Field(default='password', alias='MINIO_PASSWORD')
    verify_ssl: bool = Field(default=False, alias='S3_VERIFY_SSL')

    # Connection settings
    connect_timeout: int = Field(default=5, alias='S3_CONNECT_TIMEOUT')
    read_timeout: int = Field(default=60, alias='S3_READ_TIMEOUT')

    # Retry settings
    max_retries: int = Field(default=30, alias='S3_MAX_RETRIES')
    retry_base_delay: float = Field(default=1.0, alias='S3_RETRY_BASE_DELAY')
    retry_max_delay: float = Field(default=30.0, alias='S3_RETRY_MAX_DELAY')

    # Bucket names
    partial_results_bucket: str = Field(default='partialresults', alias='S3_PARTIAL_RESULTS_BUCKET')
    epoch_results_bucket: str = Field(default='epochresults', alias='S3_EPOCH_RESULTS_BUCKET')
    input_files_bucket: str = Field(default='inputfiles', alias='S3_INPUT_FILES_BUCKET')


class AuthSettings(BaseSettings):
    """Authentication configuration."""

    model_config = _BASE_CONFIG

    jwt_secret_key: str = Field(
        default='dev-secret-key-change-in-production-please',
        alias='JWT_SECRET_KEY'
    )
    jwt_algorithm: str = Field(default='HS256', alias='JWT_ALGORITHM')
    access_token_expire_minutes: int = Field(default=1440, alias='JWT_EXPIRE_MINUTES')


class WorkerSettings(BaseSettings):
    """Worker process configuration."""

    model_config = _BASE_CONFIG

    prefetch_count: int = Field(default=1, alias='WORKER_PREFETCH_COUNT')
    default_time_step: float = Field(default=0.0001, alias='DEFAULT_TIME_STEP')


class LoggingSettings(BaseSettings):
    """Logging configuration."""

    model_config = _BASE_CONFIG

    level: str = Field(default='INFO', alias='LOG_LEVEL')
    format: str = Field(default='text', alias='LOG_FORMAT')


class APISettings(BaseSettings):
    """API server configuration."""

    model_config = _BASE_CONFIG

    cors_origins: str = Field(default='*', alias='CORS_ORIGINS')
    metrics_port: int = Field(default=9090, alias='METRICS_PORT')
    default_user_username: Optional[str] = Field(default=None, alias='DEFAULT_USER_USERNAME')
    default_user_password: Optional[str] = Field(default=None, alias='DEFAULT_USER_PASSWORD')


class Settings(BaseSettings):
    """Root settings container aggregating all configuration."""

    # The root container doesn't use populate_by_name (no aliased fields), but
    # keeping extra='ignore' lets unrelated env vars pass through untouched.
    model_config = SettingsConfigDict(extra='ignore')

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    rabbitmq: RabbitMQSettings = Field(default_factory=RabbitMQSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    s3: S3Settings = Field(default_factory=S3Settings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    api: APISettings = Field(default_factory=APISettings)


# Global settings instance
settings = Settings()
