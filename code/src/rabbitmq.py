# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
import functools
import logging
import os
import time
import types
from datetime import datetime

import pika
import pika.exceptions

from config import settings


logger = logging.getLogger(__name__)

# All RabbitMQ connection-related exceptions for comprehensive error handling
RABBITMQ_CONNECTION_ERRORS = (
    pika.exceptions.AMQPConnectionError,
    pika.exceptions.ConnectionClosed,
    pika.exceptions.ConnectionClosedByBroker,
    pika.exceptions.ConnectionClosedByClient,
    pika.exceptions.ChannelClosed,
    pika.exceptions.ChannelClosedByBroker,
    pika.exceptions.ChannelClosedByClient,
    pika.exceptions.StreamLostError,
)

# Also a closed channel or connection used anyway (e.g. a nack in an error
# handler after the connection dropped): a reason to reconnect, not to crash.
RABBITMQ_CHANNEL_ERRORS = RABBITMQ_CONNECTION_ERRORS + (
    pika.exceptions.ChannelWrongStateError,
    pika.exceptions.ConnectionWrongStateError,
)

NEW_QUEUE = 'new'
TO_MERGE_QUEUE = 'to_merge'
FINISHED_EPOCH_QUEUE = 'finished_epoch'
MONOLITHIC_QUEUE = 'monolithic_jobs'
GPU_MONOLITHIC_QUEUE = 'gpu_monolithic_jobs'
GPU_DISTRIBUTED_QUEUE = 'gpu_distributed'
COMPARISON_QUEUE = 'comparison_jobs'

# v1 messages that failed wait in '<queue>.retry' for RETRY_DELAY_MS (a
# per-message TTL) and are then dead-lettered back to their queue, carrying
# the number of failed deliveries in DELIVERIES_HEADER: classic queues keep no
# delivery count, and a nack-requeue puts the message straight back at the
# head of its queue, a hot loop that also starves a prefetch-1 consumer's
# other jobs. Past MAX_DELIVERIES the job is failed and the message dropped.
RETRY_SUFFIX = '.retry'
# The monolithic queues' retry queues delay a job handed back after an
# infrastructure outage (publish_later), uncounted: a monolithic job's budget
# is its retry_count.
RETRIED_QUEUES = (NEW_QUEUE, GPU_DISTRIBUTED_QUEUE, TO_MERGE_QUEUE, FINISHED_EPOCH_QUEUE,
                  MONOLITHIC_QUEUE, GPU_MONOLITHIC_QUEUE)
DELIVERIES_HEADER = 'x-nbody-failed-deliveries'
RETRY_DELAY_MS = int(os.environ.get('NBODY_V1_RETRY_DELAY_MS', '15000'))
MAX_DELIVERIES = int(os.environ.get('NBODY_MAX_TASK_DELIVERIES', '5'))


class BrokerUnavailable(Exception):
    """The API cannot publish now (broker unreachable, or its publisher
    channel busy for too long): answer 503, the client may retry."""


def _short_connection(timeout_s: float, heartbeat_s: int = None) -> pika.BlockingConnection:
    """One connection attempt bounded by timeout_s, instead of
    get_rabbitmq_connection's minutes of retries. heartbeat_s None takes the
    broker's."""
    cfg = settings.rabbitmq
    return pika.BlockingConnection(pika.ConnectionParameters(
        host=cfg.host, port=cfg.port,
        credentials=pika.credentials.PlainCredentials(cfg.user, cfg.password),
        connection_attempts=1, socket_timeout=timeout_s,
        blocked_connection_timeout=timeout_s, stack_timeout=timeout_s,
        heartbeat=heartbeat_s,
    ))


def publisher_connection(timeout_s: float = 5.0, heartbeat_s: int = 30) -> pika.BlockingConnection:
    """A connection for a request-path publisher (the API): one attempt, and
    its own short heartbeat rather than RABBITMQ_HEARTBEAT (1800 s in prod,
    for workers that compute between I/O), so a half-open connection fails a
    confirm wait within ~2 heartbeats instead of tens of minutes. A broker that
    blocks publishers (memory/disk alarm) closes it after timeout_s."""
    return _short_connection(timeout_s, heartbeat_s)


def rabbitmq_ping(timeout_s: float = 3.0) -> None:
    """One short connection attempt (for health checks): raises if the broker
    cannot be reached within timeout_s."""
    _short_connection(timeout_s).close()


def queue_depths(timeout_s: float = 3.0) -> dict:
    """Messages ready in each work queue and each retry queue ({queue: count},
    keyed by queue name, e.g. 'new' and 'new.retry', None for a queue that
    does not exist yet). Raises if the broker cannot be reached."""
    connection = _short_connection(timeout_s)
    depths = {}
    try:
        for queue in (NEW_QUEUE, TO_MERGE_QUEUE, FINISHED_EPOCH_QUEUE, MONOLITHIC_QUEUE,
                      GPU_MONOLITHIC_QUEUE, GPU_DISTRIBUTED_QUEUE, COMPARISON_QUEUE,
                      *(q + RETRY_SUFFIX for q in RETRIED_QUEUES)):
            channel = connection.channel()
            try:
                depths[queue] = channel.queue_declare(queue=queue, passive=True).method.message_count
                channel.close()
            except pika.exceptions.ChannelClosedByBroker:     # 404: not declared yet
                depths[queue] = None
    finally:
        connection.close()
    return depths


def get_rabbitmq_connection(max_retries=None, retry_delay=None):
    """A RabbitMQ connection, retrying (max_retries attempts, retry_delay
    seconds apart, by default from the config). Raises once every attempt
    failed."""
    cfg = settings.rabbitmq
    max_retries = max_retries or cfg.max_retries
    retry_delay = retry_delay or cfg.retry_delay

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Attempting to connect to RabbitMQ at {cfg.host} (attempt {attempt}/{max_retries})")
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(
                    host=cfg.host,
                    port=cfg.port,
                    credentials=pika.credentials.PlainCredentials(cfg.user, cfg.password),
                    connection_attempts=cfg.connection_attempts,
                    retry_delay=cfg.retry_delay,
                    heartbeat=cfg.heartbeat,
                    blocked_connection_timeout=cfg.blocked_connection_timeout,
                )
            )
            logger.info("Successfully connected to RabbitMQ")
            return connection
        except RABBITMQ_CONNECTION_ERRORS as e:
            if attempt < max_retries:
                logger.warning(f"Failed to connect to RabbitMQ: {e}. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error(f"Failed to connect to RabbitMQ after {max_retries} attempts")
                raise Exception(f"Could not connect to RabbitMQ at {cfg.host} after {max_retries} attempts") from e


def ensure_rabbitmq_queues(channel):
    """Declare all required queues as durable."""
    logger.info("Declaring RabbitMQ queues")
    queues = [
        NEW_QUEUE,
        TO_MERGE_QUEUE,
        FINISHED_EPOCH_QUEUE,
        MONOLITHIC_QUEUE,
        GPU_MONOLITHIC_QUEUE,
        GPU_DISTRIBUTED_QUEUE,
        COMPARISON_QUEUE,
    ]
    declarations = [(q, None) for q in queues] + [
        (q + RETRY_SUFFIX, {'x-dead-letter-exchange': '', 'x-dead-letter-routing-key': q})
        for q in RETRIED_QUEUES]
    for queue_name, arguments in declarations:
        try:
            channel.queue_declare(queue=queue_name, durable=True, arguments=arguments)
        except pika.exceptions.ChannelClosedByBroker as e:
            if e.reply_code == 406:
                logger.error(
                    f"Queue '{queue_name}' exists with different parameters. "
                    f"Delete the queue or update parameters to match: {e}"
                )
            raise
    logger.info("All queues declared successfully")


def close_quietly(connection) -> None:
    """Close a connection being abandoned for a new one: left open, it would
    keep holding its unacknowledged messages until the broker times it out."""
    try:
        if connection is not None and connection.is_open:
            connection.close()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Error closing an abandoned RabbitMQ connection: {e}")


def delivery_count(properties) -> int:
    """How often a v1 message has been delivered, this delivery included
    (1 + the failed deliveries retry_later recorded). A broker redelivery
    after a consumer died is not counted: classic queues do not count them."""
    headers = getattr(properties, 'headers', None) or {}
    try:
        return int(headers.get(DELIVERIES_HEADER, 0)) + 1
    except (TypeError, ValueError):
        return 1


def retry_later(channel, delivery_tag: int, queue: str, body, properties, count: bool = True) -> None:
    """Requeue a v1 message that failed: published to queue's retry queue,
    back in `queue` after RETRY_DELAY_MS (count: as one more failed delivery),
    then acknowledged. A crash between the publish and the ack duplicates the
    message, which v1 handling tolerates (package, merge and epoch steps are
    idempotent)."""
    headers = dict(getattr(properties, 'headers', None) or {})
    if count:
        headers[DELIVERIES_HEADER] = delivery_count(properties)
    channel.basic_publish(
        exchange='', routing_key=queue + RETRY_SUFFIX, body=body,
        properties=pika.BasicProperties(delivery_mode=2, headers=headers,
                                        expiration=str(RETRY_DELAY_MS)))
    channel.basic_ack(delivery_tag=delivery_tag)


def declare_retry_queue(channel, queue: str) -> None:
    """Declare queue's retry queue as ensure_rabbitmq_queues does, for a
    consumer that declares only its own queue (the monolithic worker)."""
    channel.queue_declare(queue=queue + RETRY_SUFFIX, durable=True,
                          arguments={'x-dead-letter-exchange': '', 'x-dead-letter-routing-key': queue})


def publish_later(channel, queue: str, body, delay_ms: int = RETRY_DELAY_MS, headers: dict = None) -> None:
    """Publish a message that reaches `queue` only after delay_ms, through
    queue's retry queue (no acknowledgement, unlike retry_later)."""
    channel.basic_publish(
        exchange='', routing_key=queue + RETRY_SUFFIX, body=body,
        properties=pika.BasicProperties(delivery_mode=2, expiration=str(delay_ms), headers=headers))


# A monolithic job handed back by its worker carries who handed it back and
# when, so the message's consumer may take over that worker's lease even if
# the hand-back could not expire it (database down). retry_later keeps headers.
HANDBACK_WORKER_HEADER = 'x-nbody-handed-back-by'
HANDBACK_AT_HEADER = 'x-nbody-handed-back-at'


def handback_headers(worker_id: str, at) -> dict:
    """Headers of a job handed back by worker_id at `at` (an aware datetime)."""
    return {HANDBACK_WORKER_HEADER: worker_id, HANDBACK_AT_HEADER: at.isoformat()}


def handback_marker(properties):
    """(worker_id, handed-back-at datetime) of a handed-back job's message,
    or None (any other message, or unreadable headers)."""
    headers = getattr(properties, 'headers', None) or {}
    worker, at = headers.get(HANDBACK_WORKER_HEADER), headers.get(HANDBACK_AT_HEADER)
    if isinstance(worker, bytes):
        worker = worker.decode()
    if isinstance(at, bytes):
        at = at.decode()
    if not worker or not at:
        return None
    try:
        return str(worker), datetime.fromisoformat(str(at))
    except ValueError:
        return None


class ConnectionThreadChannel:
    """The channel as seen from a worker thread (a monolithic job, a comparison).

    pika's BlockingConnection is not thread safe: a job thread must not touch
    the channel. Acknowledgements and stop requests are handed to the
    connection thread with add_callback_threadsafe, which runs them in order
    from its I/O loop. That loop keeps servicing heartbeats while the job
    computes. `connection.sleep` becomes time.sleep, since the heartbeats no
    longer depend on the job thread.
    """

    def __init__(self, channel):
        self._channel = channel
        self._connection = channel.connection
        self.connection = types.SimpleNamespace(sleep=time.sleep)

    def _call(self, fn, *args, **kwargs):
        self._connection.add_callback_threadsafe(functools.partial(fn, *args, **kwargs))

    def basic_ack(self, **kwargs):
        self._call(self._channel.basic_ack, **kwargs)

    def basic_nack(self, **kwargs):
        self._call(self._channel.basic_nack, **kwargs)

    def basic_publish(self, **kwargs):
        self._call(self._channel.basic_publish, **kwargs)

    def stop_consuming(self):
        self._call(self._channel.stop_consuming)

    def on_connection_thread(self, fn):
        self._connection.add_callback_threadsafe(fn)
