# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The v2 MessageBackend: tasks from a Redis stream (v2/redis_service.py)."""

import logging
import os
from typing import Dict, Optional

from config import settings
from constants import SOFTENING_SQ
from v2.redis_service import STALE_TASK_MS, TASKS_STREAM, RedisService, get_redis_service
from workers.base.message_backend import MessageBackend, MessageBackendError, TaskHeartbeat, TaskMessage
from workers.renew_loop import RenewLoop


# Delay before a task this worker gave up on (nack) is redelivered, after its
# first delivery. It grows fourfold per delivery up to NACK_RETRY_MAX_MS.
NACK_RETRY_DELAY_MS = int(os.environ.get('NBODY_NACK_RETRY_DELAY_MS', '15000'))
NACK_RETRY_MAX_MS = int(os.environ.get('NBODY_NACK_RETRY_MAX_MS', '300000'))

logger = logging.getLogger(__name__)


class RedisBackend(MessageBackend):
    """Consumes `stream`: TASKS_STREAM for CPU workers, GPU_TASKS_STREAM for
    GPU workers."""

    reclaims_stale_tasks = True    # XAUTOCLAIM after stale_task_ms

    def __init__(self, stream: str = TASKS_STREAM, stale_task_ms: Optional[int] = None):
        self._redis: Optional[RedisService] = None
        self._stream = stream
        # Idle time after which a pending task counts as stale (its worker
        # died) and is reclaimed. keep_alive refreshes it at a third of that.
        self.stale_task_ms = stale_task_ms or STALE_TASK_MS
        # A task this worker gave up on (nack) is redelivered after this delay
        # (growing per delivery, retry_delay_ms), independent of the stale
        # threshold: it waits parked outside the stream (RedisService.park_task).
        self.nack_retry_ms = NACK_RETRY_DELAY_MS
        self.nack_retry_max_ms = NACK_RETRY_MAX_MS
        self._consumer: Optional[str] = None

    @property
    def name(self) -> str:
        return 'redis'

    def connect(self) -> None:
        """Get Redis service connection."""
        try:
            self._redis = get_redis_service()
            logger.info(f"Redis backend connected (stream: {self._stream})")
        except Exception as e:
            raise MessageBackendError(self.name, f"Failed to connect: {e}", recoverable=True)

    def disconnect(self) -> None:
        """Redis connection is managed by RedisService singleton."""
        self._redis = None

    def is_connected(self) -> bool:
        """Check if connection is active."""
        if self._redis is None:
            return False
        return self._redis.ping()

    def ensure_queues(self) -> None:
        """Ensure consumer group exists for Redis Streams."""
        if self._redis is None:
            raise MessageBackendError(self.name, "Not connected", recoverable=True)
        self._redis.ensure_consumer_group(stream=self._stream)

    def claim_task(
        self,
        consumer_name: str,
        queue_name: str,
        block_ms: int = 5000
    ) -> Optional[TaskMessage]:
        """Claim a task from the Redis stream."""
        if self._redis is None:
            raise MessageBackendError(self.name, "Not connected", recoverable=True)

        self._consumer = consumer_name
        try:
            result = self._redis.claim_task(
                consumer_name=consumer_name,
                block_ms=block_ms,
                stream=self._stream
            )

            if result is None:
                return None

            msg_id, task_dict = result
            return self._parse_or_discard(msg_id, task_dict)

        except Exception as e:
            raise MessageBackendError(self.name, f"Failed to claim task: {e}", recoverable=True)

    def claim_stale_task(
        self,
        consumer_name: str,
        queue_name: str,
        min_idle_ms: int = None
    ) -> Optional[TaskMessage]:
        """Reclaim a stale task from a crashed worker."""
        if self._redis is None:
            raise MessageBackendError(self.name, "Not connected", recoverable=True)

        min_idle_ms = min_idle_ms or self.stale_task_ms
        self._consumer = consumer_name

        try:
            result = self._redis.claim_stale_task(
                consumer_name=consumer_name,
                min_idle_ms=min_idle_ms,
                stream=self._stream
            )

            if result is None:
                return None

            msg_id, task_dict = result
            return self._parse_or_discard(msg_id, task_dict)

        except Exception as e:
            logger.warning(f"Failed to claim stale task: {e}")
            return None

    def ack_task(self, message: TaskMessage) -> None:
        """Acknowledge task completion."""
        if self._redis is None:
            raise MessageBackendError(self.name, "Not connected", recoverable=True)

        try:
            self._redis.ack_task(message.message_id, stream=self._stream)
        except Exception as e:
            raise MessageBackendError(self.name, f"Failed to ack: {e}", recoverable=True)

    def keep_alive(self, message: TaskMessage, consumer_name: str) -> Optional[TaskHeartbeat]:
        """Refresh the task's pending idle time while it is processed, so
        XAUTOCLAIM reclaims only tasks of dead workers: a slow but live
        worker's task is neither computed twice nor counted as a redelivery
        towards the delivery cap."""
        if self._redis is None:
            return None
        return _StreamTaskHeartbeat(self._redis, self._stream, consumer_name,
                                    message.message_id, self.stale_task_ms)

    def nack_task(self, message: TaskMessage, requeue: bool = True, count: bool = True) -> None:
        """Reject a task. requeue: it is parked and redelivered after
        retry_delay_ms (the delivery cap still bounds the retries: Redis
        counts every delivery, so `count` is ignored). Otherwise it is
        acknowledged and dropped."""
        if not requeue:
            self.ack_task(message)
            return
        if self._redis is None or self._consumer is None:
            return                     # still pending: reclaimed once stale
        try:
            parked = self._redis.park_task(self._consumer, message.message_id,
                                           self.retry_delay_ms(message), self._deliveries(message),
                                           stream=self._stream)
            if not parked:
                logger.info(f"Task {message.message_id} is no longer ours, not re-queued")
        except Exception as e:  # noqa: BLE001 — still pending: falls back to the stale reclaim
            logger.warning(f"Could not schedule the retry of task {message.message_id}: {e}")

    @staticmethod
    def _deliveries(message: TaskMessage) -> int:
        """How often the task has been delivered, this delivery included."""
        try:
            return max(1, int((message.raw_data or {}).get('__deliveries__', 1) or 1))
        except (TypeError, ValueError):
            return 1

    def retry_delay_ms(self, message: TaskMessage) -> int:
        """Delay before a nacked task is redelivered: nack_retry_ms after its
        first delivery, four times longer after each further one, at most
        nack_retry_max_ms (15 s, 60 s, 240 s, 300 s... by default). Every
        delivery counts towards the cap here, so a fixed short delay spent the
        whole budget in about a minute and a half. This keeps a failure that
        clears within minutes (memory pressure, a node scaling up) from
        failing the job."""
        return min(self.nack_retry_ms * 4 ** (self._deliveries(message) - 1), self.nack_retry_max_ms)

    @property
    def stream(self) -> str:
        """The task stream this backend consumes (CPU or GPU)."""
        return self._stream

    def get_redis_service(self) -> RedisService:
        """Get the underlying Redis service for advanced operations."""
        if self._redis is None:
            raise MessageBackendError(self.name, "Not connected", recoverable=True)
        return self._redis

    def _parse_or_discard(self, msg_id: str, task_dict: Dict[str, str]) -> Optional[TaskMessage]:
        """Parse a stream entry. Acknowledge (drop) one that cannot be parsed,
        which would otherwise stay pending and be reclaimed forever, blocking
        the reclaim of every other stale task (XAUTOCLAIM returns it first)."""
        task = self._parse_task(msg_id, task_dict)
        if task is None:
            logger.error(f"Discarding malformed task {msg_id}: {task_dict}")
            try:
                self._redis.ack_task(msg_id, stream=self._stream)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Could not discard malformed task {msg_id}: {e}")
        return task

    def _parse_task(self, msg_id: str, task_dict: Dict[str, str]) -> Optional[TaskMessage]:
        """Parse Redis stream entry to TaskMessage."""
        try:
            def get_int(key: str, default: int = 0) -> int:
                val = task_dict.get(key)
                return int(val) if val else default

            def get_float(key: str, default: float = 0.0) -> float:
                val = task_dict.get(key)
                return float(val) if val else default

            def get_str(key: str, default: str = '') -> str:
                return task_dict.get(key, default)

            def get_bool(key: str, default: bool = False) -> bool:
                val = task_dict.get(key, '').lower()
                return val in ('true', '1', 'yes')

            return TaskMessage(
                message_id=msg_id,
                job_id=get_int('job_id'),
                bucket=get_str('bucket', settings.s3.input_files_bucket),
                file_key=get_str('file_key'),
                epoch_num=get_int('epoch'),
                package_num=get_int('package_num'),
                package_id=get_int('package_id') if task_dict.get('package_id') else None,
                package_size=get_int('package_size'),
                total_packages=get_int('total_packages', 1),
                num_epochs=get_int('num_epochs'),
                time_step=get_float('time_step', 0.0001),
                snapshot_interval=get_int('snapshot_interval', 5),
                algorithm=get_str('force_algorithm', 'direct'),
                barnes_hut_theta=get_float('barnes_hut_theta', 0.5),
                softening_sq=get_float('softening_sq', SOFTENING_SQ),
                integration_method=get_str('integration_method', 'symplectic_euler'),
                gpu_enabled=get_bool('gpu_enabled'),
                adaptive_enabled=get_bool('adaptive_enabled'),
                adaptive_eta=get_float('adaptive_eta', 0.02),
                adaptive_dt_min=get_float('adaptive_dt_min', 1e-10),
                adaptive_dt_max=get_float('adaptive_dt_max', 0.1),
                start_idx=get_int('start_idx', -1),
                end_idx=get_int('end_idx', -1),
                generation=get_int('generation', 0),
                raw_data=task_dict
            )
        except (ValueError, KeyError, TypeError) as e:
            logger.error(f"Failed to parse task: {e}")
            return None


class _StreamTaskHeartbeat(RenewLoop, TaskHeartbeat):
    """Refreshes a claimed task's idle time every third of the stale threshold
    (see RenewLoop). Started at once. Stops by itself once the task is no
    longer pending for this consumer (acked, or reclaimed by another one)."""

    name = 'task-heartbeat'
    log = logger

    def __init__(self, redis_service: RedisService, stream: str, consumer_name: str,
                 msg_id: str, stale_task_ms: int):
        super().__init__(stale_task_ms / 3000.0)
        self._rs = redis_service
        self._args = (consumer_name, msg_id, stream)
        self.what = f"Task heartbeat for {msg_id}"
        self.start()

    def renew(self) -> bool:
        return self._rs.touch_task(*self._args)
