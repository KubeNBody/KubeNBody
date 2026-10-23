# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Data schemas for V2 message passing and job configuration.
"""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict

from constants import SOFTENING_SQ


# String-encoded default for fields that round-trip through Redis streams.
_SOFTENING_SQ_STR = repr(SOFTENING_SQ)


class JobStatusV2(Enum):
    """Status states for V2 jobs."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class TaskV2:
    """Task message for V2 worker stream.

    All fields are strings because Redis Streams store string fields.
    Workers must convert to appropriate types.
    """
    job_id: str
    epoch: str
    package_num: str
    start_idx: str
    end_idx: str
    total_packages: str
    time_step: str
    snapshot_interval: str
    num_epochs: str
    # Bodies per package (all but the last). Lets the merger check partial sizes
    package_size: str = "0"
    # Routes the next epoch to the GPU stream (with package_size, spares the
    # merger a read of the job meta)
    gpu_enabled: str = "false"
    # Algorithm selection: 'direct' (O(N^2)) or 'barnes_hut' (O(N log N))
    force_algorithm: str = "direct"
    barnes_hut_theta: str = "0.5"  # Only used if barnes_hut
    softening_sq: str = _SOFTENING_SQ_STR  # Gravitational softening (epsilon squared)
    # Integration method: an integrators.IntegrationMethod value
    integration_method: str = "symplectic_euler"
    # Adaptive timestepping (softening-length criterion, dt = eta*sqrt(eps/a_max))
    adaptive_enabled: str = "false"
    adaptive_eta: str = "0.02"
    adaptive_dt_min: str = "1e-10"
    adaptive_dt_max: str = "0.1"
    # Generation the task was issued under (epoch fencing, see redis_service)
    generation: str = "0"

    def to_stream_dict(self) -> Dict[str, str]:
        """Convert to dict suitable for Redis XADD."""
        return asdict(self)


@dataclass
class JobMetaV2:
    """Job metadata stored in Redis hash.

    This is the in-memory representation. When stored in Redis,
    all values are converted to strings.
    """
    job_id: int
    num_epochs: int
    current_epoch: int
    time_step: float
    total_packages: int
    package_size: int
    snapshot_interval: int
    status: str
    input_bucket: str
    input_file_key: str
    # Algorithm selection: 'direct' (O(N^2)) or 'barnes_hut' (O(N log N))
    force_algorithm: str = "direct"
    barnes_hut_theta: float = 0.5  # Only used if barnes_hut
    softening_sq: float = SOFTENING_SQ  # Gravitational softening (epsilon squared)
    # Integration method: an integrators.IntegrationMethod value
    integration_method: str = "symplectic_euler"
    # Adaptive timestepping
    adaptive_enabled: bool = False
    adaptive_eta: float = 0.02
    adaptive_dt_min: float = 1e-10
    adaptive_dt_max: float = 0.1
    # GPU acceleration
    gpu_enabled: bool = False
    # Package partitioning: 'linear' (file order), 'random' (seeded shuffle),
    # 'spatial' (Morton/Z-order) or 'orb' (orthogonal recursive bisection). See
    # v2.partitioning. Permute-once at create. Recorded for repro.
    partition_strategy: str = "linear"
    partition_seed: int = 0
    # Epoch fencing (see redis_service): bumped on every recovery. Tasks carry
    # the generation they were issued under.
    generation: int = 0
    # Last epoch whose merged state is in the bodies key (0 = initial state).
    committed_epoch: int = 0
    # Step size of the epoch in flight (differs from time_step with adaptive dt).
    epoch_dt: float = 0.0

    def to_redis_dict(self) -> Dict[str, str]:
        """Convert to dict suitable for Redis HSET."""
        return {k: str(v) for k, v in asdict(self).items()}

