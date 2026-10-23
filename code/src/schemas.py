# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Data schemas for message passing and model statuses.
"""
import json
from dataclasses import asdict, dataclass, field
from typing import Union

from constants import SOFTENING_SQ
from enums import JobStatus, JobType, PackageStatus  # noqa: F401 -- re-exported for backwards compat


SCHEMA_VERSION = 1


def decode_message_body(body: Union[str, bytes]) -> str:
    """Decode message body to string, handling both str and bytes."""
    if isinstance(body, bytes):
        return body.decode('utf-8')
    return body


# Message Dataclasses
@dataclass
class WorkerMessage:
    """Message sent to worker queue for processing packages."""
    job_id: int
    bucket: str
    file_key: str
    epoch_num: int
    package_num: int
    package_id: int
    package_size: int
    num_of_epochs: int
    time_step: float
    # Algorithm selection: 'direct' (O(N^2)) or 'barnes_hut' (O(N log N))
    force_algorithm: str = "direct"
    barnes_hut_theta: float = 0.5  # Opening angle for Barnes-Hut
    softening_sq: float = SOFTENING_SQ  # Gravitational softening (epsilon squared)
    # Integration method: an integrators.IntegrationMethod value
    integration_method: str = "symplectic_euler"
    # Adaptive timestepping
    adaptive_enabled: bool = False
    adaptive_eta: float = 0.02
    adaptive_dt_min: float = 0.0
    adaptive_dt_max: float = 0.0
    version: int = field(default=SCHEMA_VERSION)

    def to_json(self) -> str:
        """Convert to JSON string for RabbitMQ."""
        return json.dumps(asdict(self))

    @classmethod
    def for_package(cls, job, epoch_num: int, package_num: int, package_id: int,
                    bucket: str, file_key: str, time_step: float) -> "WorkerMessage":
        """The message of one package of a v1 job's epoch: the job's physics
        settings, the state to start from (bucket/file_key) and the epoch's step."""
        return cls(
            job_id=job.job_id, bucket=bucket, file_key=file_key, epoch_num=epoch_num,
            package_num=package_num, package_id=package_id, package_size=job.package_size,
            num_of_epochs=job.num_epochs, time_step=time_step,
            force_algorithm=job.force_algorithm, barnes_hut_theta=job.barnes_hut_theta,
            softening_sq=job.softening_sq, integration_method=job.integration_method,
            adaptive_enabled=job.adaptive_enabled, adaptive_eta=job.adaptive_eta or 0.02,
            adaptive_dt_min=job.adaptive_dt_min or 0.0, adaptive_dt_max=job.adaptive_dt_max or 0.0,
        )

    @classmethod
    def from_json(cls, body: Union[str, bytes]) -> "WorkerMessage":
        """Create from JSON string or bytes."""
        data = json.loads(decode_message_body(body))
        data.setdefault('version', 1)
        data.setdefault('force_algorithm', 'direct')
        data.setdefault('barnes_hut_theta', 0.5)
        data.setdefault('softening_sq', SOFTENING_SQ)
        data.setdefault('integration_method', 'symplectic_euler')
        data.setdefault('adaptive_enabled', False)
        data.setdefault('adaptive_eta', 0.02)
        data.setdefault('adaptive_dt_min', 0.0)
        data.setdefault('adaptive_dt_max', 0.0)
        return cls(**data)


@dataclass
class MergerMessage:
    """Message sent to merger queue for combining partial results."""
    job_id: int
    bucket: str
    file_key: str
    epoch_num: int
    package_num: int
    package_id: int
    package_size: int
    local_a_max: float = 0.0  # For adaptive timestepping: max acceleration in this package
    version: int = field(default=SCHEMA_VERSION)

    def to_json(self) -> str:
        """Convert to JSON string for RabbitMQ."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, body: Union[str, bytes]) -> "MergerMessage":
        """Create from JSON string or bytes."""
        data = json.loads(decode_message_body(body))
        data.setdefault('version', 1)
        data.setdefault('local_a_max', 0.0)
        return cls(**data)


@dataclass
class FinishedEpochMessage:
    """Message sent to orchestrator when epoch is complete."""
    job_id: int
    bucket: str
    file_key: str
    epoch_num: int
    a_max: float = 0.0  # For adaptive timestepping: global max acceleration from merger
    version: int = field(default=SCHEMA_VERSION)

    def to_json(self) -> str:
        """Convert to JSON string for RabbitMQ."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, body: Union[str, bytes]) -> "FinishedEpochMessage":
        """Create from JSON string or bytes."""
        data = json.loads(decode_message_body(body))
        data.setdefault('version', 1)
        data.setdefault('a_max', 0.0)
        return cls(**data)


@dataclass
class MonolithicJobMessage:
    """Message sent to monolithic worker queue for processing entire jobs."""
    job_id: int
    bucket: str
    file_key: str
    num_of_epochs: int
    time_step: float
    snapshot_interval: int
    gpu_enabled: bool = False
    # Algorithm selection: 'direct' (O(N^2)) or 'barnes_hut' (O(N log N))
    force_algorithm: str = "direct"
    barnes_hut_theta: float = 0.5  # Opening angle for Barnes-Hut
    softening_sq: float = SOFTENING_SQ  # Gravitational softening (epsilon squared)
    # Integration method: an integrators.IntegrationMethod value
    integration_method: str = "symplectic_euler"
    # Adaptive timestepping (softening-length criterion, dt = eta*sqrt(eps/a_max))
    adaptive_enabled: bool = False
    adaptive_eta: float = 0.02
    adaptive_dt_min: float = 0.0   # 0.0 = use default (1e-10)
    adaptive_dt_max: float = 0.0   # 0.0 = use time_step as max
    version: int = field(default=SCHEMA_VERSION)

    def to_json(self) -> str:
        """Convert to JSON string for RabbitMQ."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, body: Union[str, bytes]) -> "MonolithicJobMessage":
        """Create from JSON string or bytes."""
        data = json.loads(decode_message_body(body))
        data.setdefault('version', 1)
        data.setdefault('gpu_enabled', False)
        data.setdefault('force_algorithm', 'direct')
        data.setdefault('barnes_hut_theta', 0.5)
        data.setdefault('softening_sq', SOFTENING_SQ)
        data.setdefault('integration_method', 'symplectic_euler')
        data.setdefault('adaptive_enabled', False)
        data.setdefault('adaptive_eta', 0.02)
        data.setdefault('adaptive_dt_min', 0.0)
        data.setdefault('adaptive_dt_max', 0.0)
        return cls(**data)

    @classmethod
    def from_job(cls, job) -> "MonolithicJobMessage":
        """The execution message of a persisted job. Resume, the watchdog and
        manual regeneration all republish through this, so none of them can
        drop part of the configuration (e.g. the adaptive settings, which
        would otherwise default to a fixed step)."""
        return cls(
            job_id=job.job_id,
            bucket=job.input_bucket,
            file_key=job.input_file_key,
            num_of_epochs=job.num_epochs,
            time_step=job.time_step,
            snapshot_interval=job.snapshot_interval,
            gpu_enabled=bool(job.gpu_enabled),
            force_algorithm=job.force_algorithm,
            barnes_hut_theta=job.barnes_hut_theta,
            softening_sq=job.softening_sq if job.softening_sq is not None else SOFTENING_SQ,
            integration_method=job.integration_method,
            adaptive_enabled=bool(job.adaptive_enabled),
            adaptive_eta=job.adaptive_eta or 0.02,
            adaptive_dt_min=job.adaptive_dt_min or 0.0,
            adaptive_dt_max=job.adaptive_dt_max or 0.0,
        )


@dataclass
class ComparisonMessage:
    """Message sent to comparison queue for background comparison computation."""
    comparison_id: int
    reference_job_id: int
    comparison_job_ids: str          # JSON string: "[1, 2, 3]"
    max_snapshots: int = 100
    version: int = field(default=SCHEMA_VERSION)

    def to_json(self) -> str:
        """Convert to JSON string for RabbitMQ."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, body: Union[str, bytes]) -> "ComparisonMessage":
        """Create from JSON string or bytes."""
        data = json.loads(decode_message_body(body))
        data.setdefault('version', 1)
        data.setdefault('max_snapshots', 100)
        return cls(**data)