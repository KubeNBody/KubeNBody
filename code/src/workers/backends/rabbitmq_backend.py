# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The v1 MessageBackend: tasks from RabbitMQ queues (rabbitmq.py)."""

import collections
import json
import logging
from typing import Optional

import pika
import pika.channel
import pika.spec

from config import settings
from constants import SOFTENING_SQ
from rabbitmq import (
    RABBITMQ_CHANNEL_ERRORS,
    delivery_count,
    ensure_rabbitmq_queues,
    get_rabbitmq_connection,
    retry_later,
)
from workers.base.message_backend import MessageBackend, MessageBackendError, TaskMessage


logger = logging.getLogger(__name__)


class RabbitMQBackend(MessageBackend):
    """RabbitMQ-based message backend for V1 architecture."""

    def __init__(self):
        self._connection: Optional[pika.BlockingConnection] = None
        self._channel: Optional[pika.channel.Channel] = None
        self._prefetch_count: int = settings.worker.prefetch_count
        # Delivery tags are per channel: a message id carries the connection
        # it came from, so a tag of a lost channel is never acked on the new
        # one (where it would name another message, or close the channel).
        self._generation = 0
        self._consumer_queue: Optional[str] = None
        self._consumer_tag: Optional[str] = None
        self._buffer = collections.deque()      # (method, properties, body) pushed by the broker
        self._inflight = {}                     # message_id -> (queue, body, properties)

    @property
    def name(self) -> str:
        return 'rabbitmq'

    def connect(self) -> None:
        """Establish connection to RabbitMQ (a reconnect makes one attempt:
        the worker loop backs off between attempts)."""
        try:
            self._connection = get_rabbitmq_connection(max_retries=1 if self._generation else None)
            self._channel = self._connection.channel()
            # publishes to the merger return once the broker has the message,
            # and raise when it refuses it (the task is then not acknowledged)
            self._channel.confirm_delivery()
            self._channel.basic_qos(prefetch_count=self._prefetch_count)
            self._generation += 1
            self._consumer_queue = None
            self._buffer.clear()
            self._inflight.clear()
            logger.info("RabbitMQ backend connected")
        except Exception as e:  # get_rabbitmq_connection raises a plain Exception when it gives up
            raise MessageBackendError(self.name, f"Failed to connect: {e}", recoverable=True)

    def disconnect(self) -> None:
        """Close RabbitMQ connection."""
        try:
            if self._channel and self._channel.is_open:
                self._channel.close()
            if self._connection and self._connection.is_open:
                self._connection.close()
        except Exception as e:
            logger.warning(f"Error during disconnect: {e}")
        finally:
            self._channel = None
            self._connection = None
            # unacknowledged deliveries go back to the queue with the channel
            self._consumer_queue = None
            self._buffer.clear()
            self._inflight.clear()

    def is_connected(self) -> bool:
        """Check if connection is active."""
        return (
            self._connection is not None and
            self._connection.is_open and
            self._channel is not None and
            self._channel.is_open
        )

    def ensure_queues(self) -> None:
        """Ensure required queues exist."""
        if not self.is_connected():
            raise MessageBackendError(self.name, "Not connected", recoverable=True)
        ensure_rabbitmq_queues(self._channel)

    def claim_task(
        self,
        consumer_name: str,
        queue_name: str,
        block_ms: int = 5000
    ) -> Optional[TaskMessage]:
        """
        Claim a task from the queue, waiting up to block_ms for one.

        The broker pushes up to prefetch_count messages to a consumer
        (basic_consume) into a local buffer. An empty buffer waits in
        process_data_events, which returns as soon as a message arrives and
        services heartbeats meanwhile (basic_get would return at once on an
        empty queue, and idle workers would poll the broker in a hot loop).
        """
        if not self.is_connected():
            raise MessageBackendError(self.name, "Not connected", recoverable=True)

        try:
            if self._consumer_queue != queue_name:
                self._consume(queue_name)
            if not self._buffer:
                self._connection.process_data_events(time_limit=block_ms / 1000.0)
            if not self._buffer:
                return None
            method, properties, body = self._buffer.popleft()

            task = self._parse_message(method.delivery_tag, body, properties)
            if task is None:
                # would otherwise stay unacknowledged on this channel for good
                self._channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return None
            self._inflight[task.message_id] = (queue_name, body, properties)
            return task

        except RABBITMQ_CHANNEL_ERRORS as e:
            raise MessageBackendError(self.name, f"Failed to claim task: {e}", recoverable=True)

    def _consume(self, queue_name: str) -> None:
        if self._consumer_queue is not None:
            # (a worker consumes one queue, this only runs if that changes)
            self._channel.basic_cancel(self._consumer_tag)
            while self._buffer:
                self._channel.basic_nack(delivery_tag=self._buffer.popleft()[0].delivery_tag, requeue=True)
        self._consumer_tag = self._channel.basic_consume(
            queue=queue_name, auto_ack=False,
            on_message_callback=lambda ch, method, properties, body:
                self._buffer.append((method, properties, body)))
        self._consumer_queue = queue_name

    def claim_stale_task(
        self,
        consumer_name: str,
        queue_name: str,
        min_idle_ms: int = 300000
    ) -> Optional[TaskMessage]:
        """
        RabbitMQ doesn't have built-in stale task reclamation like Redis.
        Messages are requeued automatically if consumer dies without ack.
        Return None to indicate this is handled by RabbitMQ internally.
        """
        return None

    def _current_tag(self, message: TaskMessage) -> Optional[int]:
        """The message's delivery tag, or None if it came from a previous
        connection (the broker requeued it when that channel closed)."""
        generation, _, tag = message.message_id.partition(':')
        if int(generation) != self._generation:
            logger.warning(f"Task {message.message_id} came from a lost connection, "
                           f"the broker redelivers it")
            return None
        return int(tag)

    def ack_task(self, message: TaskMessage) -> None:
        """Acknowledge task completion."""
        if not self.is_connected():
            raise MessageBackendError(self.name, "Not connected", recoverable=True)

        tag = self._current_tag(message)
        if tag is None:
            return
        try:
            self._channel.basic_ack(delivery_tag=tag)
            self._inflight.pop(message.message_id, None)
        except RABBITMQ_CHANNEL_ERRORS as e:
            raise MessageBackendError(self.name, f"Failed to ack: {e}", recoverable=True)

    def nack_task(self, message: TaskMessage, requeue: bool = True, count: bool = True) -> None:
        """Reject a task. requeue: redelivered after the retry delay
        (rabbitmq.retry_later), as one more failed delivery towards the
        worker's delivery cap unless count is False. Otherwise dropped."""
        if not self.is_connected():
            raise MessageBackendError(self.name, "Not connected", recoverable=True)

        tag = self._current_tag(message)
        if tag is None:
            return
        try:
            inflight = self._inflight.pop(message.message_id, None)
            if requeue and inflight is not None:
                queue, body, properties = inflight
                retry_later(self._channel, tag, queue, body, properties, count=count)
            else:
                self._channel.basic_nack(delivery_tag=tag, requeue=requeue)
        except RABBITMQ_CHANNEL_ERRORS as e:
            raise MessageBackendError(self.name, f"Failed to nack: {e}", recoverable=True)

    def get_channel(self) -> pika.channel.Channel:
        """Get the underlying channel for advanced operations."""
        if not self.is_connected():
            raise MessageBackendError(self.name, "Not connected", recoverable=True)
        return self._channel

    def _parse_message(self, delivery_tag: int, body: bytes,
                       properties: Optional[pika.spec.BasicProperties] = None) -> Optional[TaskMessage]:
        """Parse RabbitMQ message body to TaskMessage."""
        try:
            if isinstance(body, bytes):
                body = body.decode('utf-8')
            data = json.loads(body)
            # read by the worker loop's delivery cap
            data['__deliveries__'] = delivery_count(properties)

            return TaskMessage(
                message_id=f"{self._generation}:{delivery_tag}",
                job_id=data['job_id'],
                bucket=data['bucket'],
                file_key=data['file_key'],
                epoch_num=data.get('epoch_num', 0),
                package_num=data.get('package_num', 0),
                package_id=data.get('package_id'),
                package_size=data.get('package_size', 0),
                total_packages=data.get('total_packages', 1),
                num_epochs=data.get('num_of_epochs', data.get('num_epochs', 0)),
                time_step=data.get('time_step', 0.0001),
                snapshot_interval=data.get('snapshot_interval', 5),
                algorithm=data.get('force_algorithm', 'direct'),
                barnes_hut_theta=data.get('barnes_hut_theta', 0.5),
                softening_sq=data.get('softening_sq', SOFTENING_SQ),
                integration_method=data.get('integration_method', 'symplectic_euler'),
                gpu_enabled=data.get('gpu_enabled', False),
                raw_data=data
            )
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as e:
            logger.error(f"Failed to parse message, dropping it: {e}")
            return None
