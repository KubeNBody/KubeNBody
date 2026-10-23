# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The interface of a worker's task queue, implemented by the RabbitMQ (v1)
and Redis Streams (v2) backends."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class TaskMessage:
    """
    Unified task message representation.

    This is the common format used internally by workers, regardless of
    whether the message came from RabbitMQ or Redis.
    """
    # Message identification
    message_id: str  # RabbitMQ delivery_tag or Redis stream ID

    # Job identification
    job_id: int
    bucket: str
    file_key: str

    # Task details (distributed)
    epoch_num: int
    package_num: int
    package_id: Optional[int]  # May be None for V2
    package_size: int
    total_packages: int

    # Simulation parameters
    num_epochs: int
    time_step: float
    snapshot_interval: int

    # Algorithm settings
    algorithm: str  # 'direct' or 'barnes_hut'
    barnes_hut_theta: float
    softening_sq: float  # Gravitational softening (epsilon squared)
    integration_method: str  # 'simple_euler', 'symplectic_euler', 'velocity_verlet'

    # GPU settings
    gpu_enabled: bool = False

    # Adaptive timestepping
    adaptive_enabled: bool = False
    adaptive_eta: float = 0.02
    adaptive_dt_min: float = 1e-10
    adaptive_dt_max: float = 0.1

    # Pre-computed package bounds (V2 sends these, V1 leaves them -1 so worker computes from package_size)
    start_idx: int = -1
    end_idx: int = -1

    # V2 epoch fencing: generation the task was issued under (bumped on recovery)
    generation: int = 0

    # Raw data for backend-specific operations
    raw_data: Optional[Dict[str, Any]] = None


class MessageBackend(ABC):
    """Abstract interface for message queue operations (RabbitMQ or Redis)."""

    # True if a task claimed but never acked or nacked is redelivered by
    # claim_stale_task once idle (Redis). False if it stays with this consumer
    # until the connection closes (RabbitMQ), so the worker must nack it.
    reclaims_stale_tasks: bool = False

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend identifier (e.g., 'rabbitmq', 'redis')."""
        pass

    @abstractmethod
    def connect(self) -> None:
        """Connect to the message queue, retrying internally."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Close connection to the message queue."""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connection is active."""
        pass

    @abstractmethod
    def ensure_queues(self) -> None:
        """
        Ensure required queues/streams exist.
        Creates them if they don't exist.
        """
        pass

    @abstractmethod
    def claim_task(
        self,
        consumer_name: str,
        queue_name: str,
        block_ms: int = 5000
    ) -> Optional[TaskMessage]:
        """Claim a task from queue_name, waiting up to block_ms. None if there is
        none."""
        pass

    @abstractmethod
    def claim_stale_task(
        self,
        consumer_name: str,
        queue_name: str,
        min_idle_ms: int = 300000
    ) -> Optional[TaskMessage]:
        """Reclaim a task idle for longer than min_idle_ms (a crashed worker's), if
        the backend supports it. None if there is none."""
        pass

    @abstractmethod
    def ack_task(self, message: TaskMessage) -> None:
        """Acknowledge the task's completion."""
        pass

    @abstractmethod
    def nack_task(self, message: TaskMessage, requeue: bool = True, count: bool = True) -> None:
        """Reject the task, requeued for a retry if requeue. count: whether the retry
        counts towards the delivery cap (False: an infrastructure outage, not the
        task's fault. Backends that count deliveries themselves may ignore it)."""
        pass

    def keep_alive(self, message: TaskMessage, consumer_name: str) -> Optional['TaskHeartbeat']:
        """Keep a claimed task from being reclaimed as stale while it is being
        processed (for backends that reclaim by idle time). Returns a heartbeat to
        stop() when processing ends, or None if the backend needs none (the
        default)."""
        return None

class TaskHeartbeat(ABC):
    """Handle returned by MessageBackend.keep_alive."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the heartbeat (the task is then reclaimable again once idle)."""
        pass


class MessageBackendError(Exception):
    """Raised when a message backend encounters an error."""

    def __init__(self, backend: str, message: str, recoverable: bool = True):
        self.backend = backend
        self.recoverable = recoverable
        super().__init__(f"[{backend}] {message}")
