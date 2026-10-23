# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The API's RabbitMQ publisher: one connection, used from a dedicated executor
under a lock, with publisher confirms and an idle keepalive."""

import asyncio
import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pika

from config import settings as app_settings
from db_threads import closes_connection
from logging_config import setup_logging
from rabbitmq import (
    RABBITMQ_CHANNEL_ERRORS,
    RABBITMQ_CONNECTION_ERRORS,
    BrokerUnavailable,
    close_quietly,
    ensure_rabbitmq_queues,
    publisher_connection,
)


logger = setup_logging('api.publisher')

# The API's one RabbitMQ connection. pika's BlockingConnection is not thread
# safe and blocks (a confirm waits for the broker, under flow control, a
# memory or disk alarm, a publish waits up to the connection's
# blocked_connection_timeout, then fails), so it is used only on
# _publish_executor's threads, one at a time under _publish_lock. A dedicated
# executor: publishes stuck on a broker outage must not take the default
# executor's threads, which /api/ready and the S3/DB offloads share.
rabbitmq_connection = None
rabbitmq_channel = None
_publish_lock = threading.Lock()
_publish_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='rabbitmq-publish')
# How long a publish waits for the channel (from its request) before a 503
PUBLISH_WAIT_S = 10.0
# After a failed reconnect, publishes fail at once for this long instead of
# each making its own connection attempt in turn
RECONNECT_BACKOFF_S = 5.0
# The publisher connection's heartbeat (a half-open connection fails a
# confirm wait within ~2 of these). The keepalive pumps it while idle
PUBLISHER_HEARTBEAT_S = 30
_reconnect_not_before = 0.0


def _connect_publisher(max_retries):
    """A publisher connection: one short attempt, or (max_retries None, at
    startup) retried for as long as the workers do: the broker may still be
    starting."""
    cfg = app_settings.rabbitmq
    attempts = max_retries or cfg.max_retries
    for attempt in range(1, attempts + 1):
        try:
            return publisher_connection(heartbeat_s=PUBLISHER_HEARTBEAT_S)
        except RABBITMQ_CONNECTION_ERRORS as e:
            if attempt == attempts:
                raise
            logger.warning(f"Failed to connect to RabbitMQ: {e}. Retrying in {cfg.retry_delay} seconds...")
            time.sleep(cfg.retry_delay)


def _rabbitmq_channel_locked(max_retries=None):
    """The publishing channel (publisher confirms on), reconnecting if needed.
    Caller holds _publish_lock."""
    global rabbitmq_connection, rabbitmq_channel, _reconnect_not_before

    needs_reconnect = (
        not rabbitmq_connection or
        rabbitmq_connection.is_closed or
        not rabbitmq_channel or
        rabbitmq_channel.is_closed
    )
    if not needs_reconnect:
        # An idle BlockingConnection is serviced only when used: pump its
        # I/O now (sends a due heartbeat, and raises if the broker dropped
        # the connection meanwhile, which is_closed does not show yet), so
        # the publish that follows does not fail on a dead connection.
        try:
            rabbitmq_connection.process_data_events(time_limit=0)
        except Exception as e:  # noqa: BLE001 — any failure: reconnect
            logger.warning(f"RabbitMQ connection lost while idle, reconnecting: {e}")
            needs_reconnect = True

    if needs_reconnect:
        close_quietly(rabbitmq_connection)
        rabbitmq_connection = rabbitmq_channel = None
        if max_retries is not None and time.monotonic() < _reconnect_not_before:
            raise BrokerUnavailable("RabbitMQ unreachable (recent reconnect failed)")
        try:
            rabbitmq_connection = _connect_publisher(max_retries)
            rabbitmq_channel = rabbitmq_connection.channel()
            ensure_rabbitmq_queues(rabbitmq_channel)
            # basic_publish now returns once the broker has the message, and
            # raises (NackError, UnroutableError) when it refuses it
            rabbitmq_channel.confirm_delivery()
        except Exception as e:
            close_quietly(rabbitmq_connection)
            rabbitmq_connection = rabbitmq_channel = None
            _reconnect_not_before = time.monotonic() + RECONNECT_BACKOFF_S
            raise BrokerUnavailable(f"RabbitMQ unreachable: {e}") from e

    return rabbitmq_channel


def run_on_rabbitmq_channel(fn, max_retries=1, deadline=None):
    """fn(channel) with the API's channel, holding it meanwhile. Blocking:
    call it on _publish_executor (publish_persistent, on_rabbitmq_channel).
    Raises BrokerUnavailable if the channel is not free by deadline
    (time.monotonic(), default PUBLISH_WAIT_S from now) or the broker cannot
    be reached in one short attempt, or fn fails on a dead connection or
    channel (pika's connection/channel errors, kept as __cause__). The next
    call then reconnects. Other errors of fn (a NackError or UnroutableError:
    the broker refused the message, a database error) pass through.
    max_retries None (startup) waits for the channel and retries connecting
    as long as it takes. A failure of fn still raises."""
    if max_retries is None:
        wait = -1
    else:
        wait = max(0.0, (deadline or time.monotonic() + PUBLISH_WAIT_S) - time.monotonic())
    if not _publish_lock.acquire(timeout=wait):
        raise BrokerUnavailable("RabbitMQ publisher busy")
    try:
        channel = _rabbitmq_channel_locked(max_retries)
        try:
            return fn(channel)
        except RABBITMQ_CHANNEL_ERRORS as e:
            _drop_publisher_connection_locked()
            raise BrokerUnavailable(f"RabbitMQ connection lost: {e!r}") from e
    finally:
        _publish_lock.release()


def _drop_publisher_connection_locked():
    """Forget the publisher connection (it failed mid-publish), so the next
    publish reconnects. Caller holds _publish_lock."""
    global rabbitmq_connection, rabbitmq_channel
    close_quietly(rabbitmq_connection)
    rabbitmq_connection = rabbitmq_channel = None


async def _on_publish_executor(fn, *args):
    # The deadline counts from the request: time queued for an executor
    # thread is spent waiting too
    deadline = time.monotonic() + PUBLISH_WAIT_S
    return await asyncio.get_running_loop().run_in_executor(
        _publish_executor, functools.partial(run_on_rabbitmq_channel, fn, *args, deadline=deadline))


def _publish_all(messages, channel) -> None:
    for queue, body in messages:
        channel.basic_publish(
            exchange='',
            routing_key=queue,
            body=body,
            properties=pika.BasicProperties(delivery_mode=2),   # survive a broker restart
            mandatory=True,                                     # a missing queue is an error
        )


async def publish_persistent(messages) -> None:
    """Publish (queue, body) pairs in order, off the event loop. Returns once
    the broker has confirmed every one. Raises on the first that fails (the
    ones before it are queued)."""
    await _on_publish_executor(functools.partial(_publish_all, list(messages)))


async def on_rabbitmq_channel(fn):
    """run_on_rabbitmq_channel(fn) off the event loop, for publishing code
    that also queries the database (v1_epochs.queue_v1_epoch)."""
    return await _on_publish_executor(closes_connection(fn))


def _pump_idle_publisher() -> None:
    """Service the idle publisher connection (sends its heartbeats), unless a
    publish holds it. Never reconnects (the next publish does)."""
    if not _publish_lock.acquire(blocking=False):
        return
    try:
        if rabbitmq_connection is not None and not rabbitmq_connection.is_closed:
            rabbitmq_connection.process_data_events(time_limit=0)
    except Exception as e:  # noqa: BLE001 — the next publish reconnects
        logger.info(f"Idle RabbitMQ publisher connection lost: {e}")
    finally:
        _publish_lock.release()


async def publisher_keepalive():
    """Keep the idle publisher connection alive: a BlockingConnection sends
    heartbeats only while used, so with PUBLISHER_HEARTBEAT_S the broker
    would otherwise drop it after a minute without submissions."""
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(PUBLISHER_HEARTBEAT_S / 3)
        await loop.run_in_executor(_publish_executor, _pump_idle_publisher)


async def connect_at_startup() -> None:
    """Connect the publisher (retried for as long as the workers do: the
    broker may still be starting)."""
    await asyncio.get_running_loop().run_in_executor(
        _publish_executor, run_on_rabbitmq_channel, lambda channel: None, None)


def close_publisher() -> None:
    """Close the publisher's channel and connection (blocking)."""
    global rabbitmq_connection, rabbitmq_channel
    with _publish_lock:
        if rabbitmq_channel and not rabbitmq_channel.is_closed:
            try:
                rabbitmq_channel.close()
            except Exception as e:
                logger.warning(f"Error closing channel: {e}")
        rabbitmq_channel = None
        close_quietly(rabbitmq_connection)
        rabbitmq_connection = None
