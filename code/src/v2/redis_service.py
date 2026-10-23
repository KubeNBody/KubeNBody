# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Redis service for V2 distributed worker.

Handles connection management, stream operations, and data storage
for the Redis-centric architecture.
"""

import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import redis
from redis.exceptions import ReadOnlyError
from redis.sentinel import Sentinel

from config import settings
from logging_config import setup_logging
from v2.binary_format import ACCEL_SIZE, BODY_SIZE
from v2.redis_scripts import (
    _BEGIN_MERGE,
    _BEGIN_RECOVERY,
    _CANCEL_JOB,
    _CLEANUP_EPOCH,
    _COMMIT_STAGED,
    _DELETE_FOR_GENERATION,
    _ENQUEUE_EPOCH,
    _FAIL_JOB,
    _FINALIZE_JOB,
    _MARK_STARTED,
    _PARK_TASK,
    _PAUSE_JOB,
    _PROMOTE_DUE_TASKS,
    _PUBLISH_EPOCH,
    _REGISTER_PARTIAL,
    _RELEASE_LEASE,
    _RENEW_LEASE,
    _RESTORE_STATE,
    _RESUME_JOB,
    _SHIP_ACCELERATIONS,
    _TOUCH_TASK,
)


logger = setup_logging('redis-service-v2')

cfg = settings.redis

# Key prefixes for v2
V2_PREFIX = 'v2:'


def meta_key(job_id: int) -> str:
    return f'{V2_PREFIX}job:{job_id}:meta'


def bodies_key(job_id: int) -> str:
    return f'{V2_PREFIX}bodies:{job_id}'


def accelerations_key(job_id: int) -> str:
    return f'{V2_PREFIX}accelerations:{job_id}'


def hosts_key(job_id: int) -> str:
    return f'{V2_PREFIX}job:{job_id}:hosts'


def _text(value):
    """A reply value as str (the client returns bytes)."""
    return value.decode() if isinstance(value, bytes) else value
TASKS_STREAM = f'{V2_PREFIX}tasks'
GPU_TASKS_STREAM = f'{V2_PREFIX}gpu_tasks'
CONSUMER_GROUP = 'workers'

# Timeouts and TTLs from config
STALE_TASK_MS = cfg.stale_task_ms
DEFAULT_JOB_TTL_SECONDS = cfg.default_job_ttl_seconds
# Epochs whose last package has registered but whose merge has not finished
# publishing. Members are "{job_id}:{generation}:{epoch}", scored by the time the
# epoch became mergeable. Any idle worker may take over an entry whose merge
# lease is not held (see DistributedWorkerV2._try_takeover).
PENDING_MERGES_KEY = f'{V2_PREFIX}pending_merges'

# Timeout of a Sentinel query (connect and reply): they are tiny, so a few
# seconds. A dead Sentinel is then skipped for the next one.
SENTINEL_SOCKET_TIMEOUT_S = 5
# Merge lease: short, owner-tokened, renewed by the merging worker every third
# of it while it works. A dead merger's epoch is taken over once it lapses.
MERGE_LEASE_MS = 30_000

# Registration / commit / enqueue results (negative = task or merge is stale).
STALE_GENERATION = -1
STALE_EPOCH = -2
JOB_NOT_RUNNING = -3
JOB_PAUSED = -4
# pause / resume / cancel only
JOB_MISSING = -5          # no meta hash (expired, evicted, deleted)
STATE_MISSING = -6        # meta present, but the bodies key is gone
# recovery only
RECOVERY_LOCK_LOST = -7   # the caller no longer holds the job's recovery lock

STAGING_INCOMPLETE = -8   # commit refused: the staged slices are not all there
PARTIAL_REJECTED = -9     # a result whose length does not fit its package

# A recovery holds the job's lock from listing its snapshots to queueing its
# first epoch, renewing it every RECOVERY_LOCK_RENEW_S while it runs, so a
# recovery whose process died (an API pod killed mid-recovery) frees the job
# within RECOVERY_LOCK_TTL_S rather than blocking it for a fixed long TTL. A
# recovery that loses its lock anyway is refused at its generation bump
# (another may have started).
RECOVERY_LOCK_TTL_S = 60
RECOVERY_LOCK_RENEW_S = 15


@dataclass(frozen=True)
class FenceState:
    """A job's fencing fields as read from its meta (None where absent), with
    the comparisons the Python side makes against them. They mirror the Lua
    fence: a meta without a generation is generation 0, one without a
    committed_epoch skips the epoch checks."""
    generation: Optional[str]
    committed_epoch: Optional[str]
    status: Optional[str]

    @classmethod
    def of(cls, fields: Dict[str, Optional[str]]) -> 'FenceState':
        """From get_fence_state's dict, or a whole meta dict."""
        return cls(fields.get('generation'), fields.get('committed_epoch'), fields.get('status'))

    @property
    def committed(self) -> Optional[int]:
        return None if self.committed_epoch is None else int(self.committed_epoch)

    def is_current(self, generation) -> bool:
        """True if `generation` is the job's live generation."""
        return (self.generation or '0') == str(generation)

    def committed_is(self, epoch: int) -> bool:
        """True if `epoch` is known to be the committed epoch."""
        return self.committed is not None and self.committed == epoch

    def committed_at_least(self, epoch: int) -> bool:
        """True if `epoch` is known to be committed (it or a later one is)."""
        return self.committed is not None and self.committed >= epoch

    def holds_state_of(self, generation, epoch: int) -> bool:
        """True unless the state has moved on from `epoch` of `generation`: a
        newer generation, or another committed epoch (the bodies key then holds
        another epoch's state)."""
        return self.is_current(generation) and (self.committed is None or self.committed == epoch)


_TASK_FENCE_FIELDS = ('generation', 'committed_epoch', 'status')


def _task_fence(values) -> Dict[str, Optional[str]]:
    """The fence dict of a task read (HMGET of _TASK_FENCE_FIELDS)."""
    dec = [_text(v) for v in values]
    return dict(zip(_TASK_FENCE_FIELDS, dec))


@dataclass
class MergeStart:
    """What begin_merge read when it took the merge lease: the fence, the
    previous epoch's simulation time (sim_time of the meta, None if absent)
    and, if the epoch was still to be committed in the live generation, its
    a_max values (None when not read)."""
    token: str
    fence: 'FenceState'
    sim_time: Optional[str]
    amax: Optional[List[float]]


# Per-thread count of Redis round trips (commands plus pipeline executions),
# for the timing records of task_timing. Thread-local so the merge-lease renewal
# thread does not inflate the count of the task it runs alongside.
_ROUND_TRIPS = threading.local()


def _int_or(value, default: int) -> int:
    """value as an int, or default if it is missing or not a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def rt_reset() -> None:
    _ROUND_TRIPS.n = 0


def rt_count() -> int:
    return getattr(_ROUND_TRIPS, 'n', 0)


def _count_round_trips(client) -> None:
    """Wrap a redis-py client so every command and pipeline execution
    increments the calling thread's round-trip counter."""
    if getattr(client, '_nbody_counted', False):
        return
    orig_execute = client.execute_command
    orig_pipeline = client.pipeline

    def execute_command(*args, **kwargs):
        _ROUND_TRIPS.n = getattr(_ROUND_TRIPS, 'n', 0) + 1
        return orig_execute(*args, **kwargs)

    def pipeline(*args, **kwargs):
        pipe = orig_pipeline(*args, **kwargs)
        orig_pipe_execute = pipe.execute

        def execute(*a, **k):
            _ROUND_TRIPS.n = getattr(_ROUND_TRIPS, 'n', 0) + 1
            return orig_pipe_execute(*a, **k)

        # Commands a pipeline sends on its own, outside execute(): redis-py's
        # SCRIPT EXISTS check before a pipeline that holds a script
        # (Pipeline.load_scripts), and WATCH-mode commands. Each is a round
        # trip. Without this the counter missed one per commit.
        orig_immediate = getattr(pipe, 'immediate_execute_command', None)
        if orig_immediate is not None:
            def immediate_execute_command(*a, **k):
                _ROUND_TRIPS.n = getattr(_ROUND_TRIPS, 'n', 0) + 1
                return orig_immediate(*a, **k)
            pipe.immediate_execute_command = immediate_execute_command

        pipe.execute = execute
        return pipe

    client.execute_command = execute_command
    client.pipeline = pipeline
    client._nbody_counted = True


class RedisService:
    """Centralized Redis service for V2 components.

    Features:
    - Automatic reconnection on connection loss
    - Connection pooling
    - Retry logic for transient failures
    - Sentinel HA support with automatic failover handling
    """

    def __init__(self):
        self.client: Optional[redis.Redis] = None
        self.sentinel: Optional[Sentinel] = None
        self._lock = threading.Lock()
        self._use_sentinel = cfg.sentinel_enabled
        self._reclaim_cursors = {}   # stream -> XAUTOCLAIM cursor (claim_stale_task)
        self._scripts = {}           # name -> registered Lua script (_script)
        self._connect()

    def _connect(self, max_retries: int = None, retry_delay: float = None):
        """Establish Redis connection with retry logic.

        Supports both direct connection and Sentinel-based HA.
        """
        max_retries = max_retries or cfg.max_retries
        retry_delay = retry_delay or cfg.retry_delay

        for attempt in range(max_retries):
            try:
                if self._use_sentinel:
                    self._connect_sentinel()
                else:
                    self._connect_direct()
                return
            except (redis.ConnectionError, redis.TimeoutError) as e:
                logger.warning(f"Redis connection attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    raise RuntimeError(f"Failed to connect to Redis after {max_retries} attempts")

    def _connect_direct(self):
        """Connect directly to a Redis instance (non-HA mode)."""
        self.client = redis.Redis(
            host=cfg.host,
            port=cfg.port,
            password=cfg.password,
            db=cfg.db,
            decode_responses=False,  # Binary mode for body data
            socket_timeout=cfg.socket_timeout,
            socket_connect_timeout=cfg.socket_connect_timeout,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        self.client.ping()
        _count_round_trips(self.client)
        logger.info(f"Connected to Redis (direct) at {cfg.host}:{cfg.port}")

    def _connect_sentinel(self):
        """Connect via Sentinel for HA mode."""
        sentinel_hosts = cfg.get_sentinel_hosts()

        # One Sentinel client for the service's life: a reconnect only asks it
        # for the current master again (a new client each time would leak the
        # connections of the old one).
        if self.sentinel is None:
            logger.info(f"Connecting to Redis via Sentinel: hosts={sentinel_hosts}, "
                        f"master={cfg.sentinel_master}")
            # sentinel_kwargs alone configures the connections to the
            # Sentinels (redis-py ignores the other keyword arguments for
            # them): without timeouts there, a pooled connection to a
            # Sentinel that vanished with its pod (no RST) blocks the next
            # discover_master() until the kernel gives up on TCP, ~15 min.
            self.sentinel = Sentinel(
                sentinel_hosts,
                socket_timeout=cfg.socket_connect_timeout,
                sentinel_kwargs={
                    'password': cfg.password,
                    'socket_timeout': SENTINEL_SOCKET_TIMEOUT_S,
                    'socket_connect_timeout': SENTINEL_SOCKET_TIMEOUT_S,
                    'socket_keepalive': True,
                },
            )

        # Get master connection - Sentinel handles failover automatically
        self.client = self.sentinel.master_for(
            cfg.sentinel_master,
            password=cfg.password,
            db=cfg.db,
            decode_responses=False,
            socket_timeout=cfg.socket_timeout,
            socket_connect_timeout=cfg.socket_connect_timeout,
            socket_keepalive=True,
            retry_on_timeout=True,
            health_check_interval=30,
        )

        # Verify connection
        self.client.ping()
        _count_round_trips(self.client)
        master_addr = self.sentinel.discover_master(cfg.sentinel_master)
        logger.info(f"Connected to Redis (Sentinel) master at {master_addr[0]}:{master_addr[1]}")

    def _reconnect(self, failed_client) -> None:
        """Replace a client that failed, once: threads that failed on the same
        client share one reconnect (under the lock), and the old pool's idle
        connections are closed instead of leaking. Connections other threads
        are using are left alone: closing them would break their in-flight
        commands (a slow GET timing out here says nothing about theirs), and
        a retried non-idempotent command could apply twice."""
        with self._lock:
            if self.client is not failed_client:
                return  # another thread already reconnected
            try:
                self._connect(max_retries=3, retry_delay=1.0)
            except Exception as e:  # noqa: BLE001 — the next attempt retries
                logger.warning(f"Reconnection failed: {e}")
                return
        if self.client is not failed_client:
            try:
                failed_client.connection_pool.disconnect(inuse_connections=False)
            except Exception:  # noqa: BLE001 — best effort
                pass

    def _execute_with_failover_retry(self, operation, *args, **kwargs):
        """operation(*args, **kwargs), retried with exponential backoff while
        Sentinel elects a new master: ReadOnlyError (a write to a replica during
        failover), ConnectionError (master unreachable), TimeoutError. RuntimeError
        once the retries are spent."""
        if not self._use_sentinel:
            # Non-HA mode: just execute directly
            return operation(*args, **kwargs)

        last_error = None
        max_retries = cfg.failover_max_retries
        base_delay = cfg.failover_base_delay

        for attempt in range(max_retries):
            client = self.client
            try:
                return operation(*args, **kwargs)
            except (ReadOnlyError, redis.ConnectionError, redis.TimeoutError) as e:
                last_error = e
                delay = min(base_delay * (2 ** attempt), 10)  # Cap at 10s

                if isinstance(e, ReadOnlyError):
                    logger.warning(f"Redis master failover in progress (attempt {attempt + 1}/{max_retries}), "
                                   f"retrying in {delay:.1f}s: {e}")
                else:
                    logger.warning(f"Redis connection error (attempt {attempt + 1}/{max_retries}), "
                                   f"retrying in {delay:.1f}s: {e}")

                time.sleep(delay)
                self._reconnect(client)  # to the new master

        raise RuntimeError(f"Redis operation failed after {max_retries} retries: {last_error}")

    def ping(self) -> bool:
        """Health check."""
        try:
            return self.client.ping()
        except (redis.ConnectionError, redis.TimeoutError):
            return False

    # Stream Operations (Task Queue)

    def ensure_consumer_group(self, stream: str = TASKS_STREAM):
        """Create consumer group if it doesn't exist."""
        def _create_group():
            try:
                self.client.xgroup_create(
                    stream,
                    CONSUMER_GROUP,
                    id='0',
                    mkstream=True
                )
                logger.info(f"Created consumer group '{CONSUMER_GROUP}' on stream '{stream}'")
            except redis.ResponseError as e:
                if "BUSYGROUP" in str(e):
                    pass
                else:
                    raise

        self._execute_with_failover_retry(_create_group)

    def claim_task(
        self,
        consumer_name: str,
        block_ms: int = 5000,
        stream: str = TASKS_STREAM
    ) -> Optional[Tuple[str, Dict[str, str]]]:
        """Claim one new task from stream, waiting up to block_ms: (message_id,
        task_dict), or None if there is none.

        Exactly one: XREADGROUP with a larger COUNT would make the extra messages
        pending for this consumer without returning them, so they sat unprocessed
        until reclaimed as stale."""
        def _claim():
            result = self.client.xreadgroup(
                CONSUMER_GROUP,
                consumer_name,
                {stream: '>'},
                count=1,
                block=block_ms
            )

            if not result:
                return None

            # result format: [(stream_name, [(msg_id, {field: value}), ...])]
            try:
                stream_name, messages = result[0]
                if not messages:
                    return None

                msg_id, fields = messages[0]

                if not isinstance(fields, dict) or not fields:
                    logger.error(f"Malformed stream entry: expected dict, got {type(fields)}")
                    return None

                # Decode bytes to strings
                msg_id_str = _text(msg_id)
                task = {
                    _text(k):
                    _text(v)
                    for k, v in fields.items()
                }
                # A retried task (park_task) carries its earlier deliveries:
                # this one is the next
                prior = task.pop('__prior_deliveries__', None)
                if prior is not None:
                    task['__deliveries__'] = str(_int_or(prior, 0) + 1)
                return msg_id_str, task
            except (ValueError, TypeError, IndexError, KeyError) as e:
                logger.error(f"Failed to parse stream entry: {e}")
                return None

        return self._with_consumer_group(stream, lambda: self._execute_with_failover_retry(_claim))

    def _with_consumer_group(self, stream: str, operation):
        """Run a consumer-group read, recreating the group once if Redis lost
        it (a restart without persistence, a flush): ensure_consumer_group
        otherwise runs only at startup, and every read would fail with NOGROUP."""
        try:
            return operation()
        except redis.ResponseError as e:
            if 'NOGROUP' not in str(e):
                raise
            logger.warning(f"Consumer group '{CONSUMER_GROUP}' missing on '{stream}', recreating it")
            self.ensure_consumer_group(stream=stream)
            return operation()

    def claim_stale_task(
        self,
        consumer_name: str,
        min_idle_ms: int = STALE_TASK_MS,
        stream: str = TASKS_STREAM
    ) -> Optional[Tuple[str, Dict[str, str]]]:
        """Reclaim a task pending for longer than min_idle_ms (a crashed worker's),
        atomically with XAUTOCLAIM: (message_id, task_dict), or None if there is
        none."""
        # Tasks given up on whose retry delay has passed go back to the stream
        # (as new entries, read by claim_task). The reclaim scan runs every
        # few seconds, so that is when they come due.
        try:
            self.promote_due_tasks(stream=stream)
        except redis.RedisError as e:
            logger.warning(f"Could not re-queue tasks due for a retry: {e}")

        # XAUTOCLAIM examines at most ten pending entries per call (COUNT 1).
        # Continuing from the returned cursor, not from 0-0 every time, reaches
        # stale entries that sit behind many live ones.
        cursors = self._reclaim_cursors

        def _autoclaim():
            try:
                result = self.client.xautoclaim(
                    stream,
                    CONSUMER_GROUP,
                    consumer_name,
                    min_idle_time=min_idle_ms,
                    start_id=cursors.get(stream, '0-0'),
                    count=1
                )
                if result:
                    nxt = result[0]
                    cursors[stream] = nxt.decode() if isinstance(nxt, bytes) else (nxt or '0-0')

                if not result or not result[1]:
                    return None

                # result[1] is list of claimed messages
                msg_id, fields = result[1][0]

                if not isinstance(fields, dict) or not fields:
                    logger.error(f"Malformed stale task entry: expected dict, got {type(fields)}")
                    return None

                msg_id_str = _text(msg_id)
                task = {
                    _text(k):
                    _text(v)
                    for k, v in fields.items()
                }
                # How often this message has been delivered (XAUTOCLAIM counts
                # as a delivery), so the worker can stop redelivering a task
                # that fails every time. The task is ours once XAUTOCLAIM
                # returned: any error here (a connection lost too) only loses
                # the count. Raising would leave the task pending for this
                # consumer, unprocessed until it went stale again.
                prior = _int_or(task.pop('__prior_deliveries__', None), 0)
                try:
                    info = self.client.xpending_range(stream, CONSUMER_GROUP, min=msg_id_str,
                                                      max=msg_id_str, count=1)
                    if info:
                        task['__deliveries__'] = str(prior + info[0]['times_delivered'])
                except redis.RedisError as e:
                    logger.warning(f"Could not read the delivery count of {msg_id_str}: {e}")
                logger.info(f"Reclaimed stale task {msg_id_str}")
                return msg_id_str, task

            except (redis.ResponseError, ValueError, TypeError, IndexError, KeyError) as e:
                if isinstance(e, redis.ResponseError) and 'NOGROUP' in str(e):
                    raise       # recreated by _with_consumer_group
                logger.warning(f"Failed to autoclaim or parse stale task: {e}")
                return None

        return self._with_consumer_group(stream, lambda: self._execute_with_failover_retry(_autoclaim))

    def ack_task(self, msg_id: str, stream: str = TASKS_STREAM):
        """Acknowledge task completion, removing from pending list."""
        self._execute_with_failover_retry(
            lambda: self.client.xack(stream, CONSUMER_GROUP, msg_id)
        )

    @staticmethod
    def retry_set_key(stream: str = TASKS_STREAM) -> str:
        """The sorted set of a stream's tasks waiting out a retry delay."""
        return f'{stream}:retry'

    def park_task(self, consumer_name: str, msg_id: str, delay_ms: int, deliveries: int,
                  stream: str = TASKS_STREAM) -> bool:
        """Give up a pending task of this consumer for a retry after delay_ms
        (any length, unlike the stale reclaim): it is acknowledged and parked
        with its delivery count (_PARK_TASK), then re-added to the stream once
        due (promote_due_tasks). False if the task is no longer this consumer's."""
        return bool(self._run_script(
            'park_task', _PARK_TASK, [stream, self.retry_set_key(stream)],
            [CONSUMER_GROUP, consumer_name, msg_id, int(delay_ms), int(deliveries)],
        ))

    def promote_due_tasks(self, stream: str = TASKS_STREAM, limit: int = 100) -> int:
        """Re-add the parked tasks whose retry delay has passed to the stream."""
        return int(self._run_script(
            'promote_due_tasks', _PROMOTE_DUE_TASKS, [stream, self.retry_set_key(stream)], [int(limit)],
        ) or 0)

    def touch_task(self, consumer_name: str, msg_id: str, stream: str = TASKS_STREAM) -> bool:
        """Reset the idle time of a task this consumer is still processing, so
        XAUTOCLAIM does not take it as stale (see _TOUCH_TASK). False once the
        task is no longer pending for this consumer."""
        return bool(self._run_script(
            'touch_task', _TOUCH_TASK, [stream], [CONSUMER_GROUP, consumer_name, msg_id],
        ))

    # Body State Operations

    def store_bodies(self, job_id: int, data: bytes, ttl_seconds: Optional[int] = None):
        """Store the job's packed body state, expiring after ttl_seconds
        (DEFAULT_JOB_TTL_SECONDS by default)."""
        def _store():
            key = bodies_key(job_id)
            # A TTL, so an abandoned job's state expires (production Redis runs
            # noeviction: memory is never reclaimed otherwise)
            ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_JOB_TTL_SECONDS
            self.client.setex(key, ttl, data)

        self._execute_with_failover_retry(_store)

    def get_bodies(self, job_id: int) -> Optional[bytes]:
        """Retrieve body state for a job."""
        key = bodies_key(job_id)
        return self._execute_with_failover_retry(lambda: self.client.get(key))

    def get_body_count(self, job_id: int) -> int:
        """Number of bodies in a job's state, without transferring it (0 if
        the state is gone)."""
        key = bodies_key(job_id)
        return self._execute_with_failover_retry(lambda: self.client.strlen(key)) // BODY_SIZE

    def get_accelerations(self, job_id: int) -> Optional[bytes]:
        """Retrieve the merger-shipped epoch-start accelerations for a job (or None)."""
        key = accelerations_key(job_id)
        return self._execute_with_failover_retry(lambda: self.client.get(key))

    def has_accelerations(self, job_id: int) -> bool:
        """Whether accelerations are shipped for the job's current state."""
        return bool(self._execute_with_failover_retry(lambda: self.client.exists(accelerations_key(job_id))))

    # Job Metadata Operations

    def set_job_meta(self, job_id: int, meta: Dict[str, Any], ttl_seconds: Optional[int] = None):
        """Store the job's metadata (values as strings), expiring after ttl_seconds
        (DEFAULT_JOB_TTL_SECONDS by default)."""
        def _set_meta():
            key = meta_key(job_id)
            str_meta = {k: str(v) for k, v in meta.items()}
            self.client.hset(key, mapping=str_meta)
            # A TTL, so an abandoned job's state expires (production Redis runs
            # noeviction: memory is never reclaimed otherwise)
            ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_JOB_TTL_SECONDS
            self.client.expire(key, ttl)

        self._execute_with_failover_retry(_set_meta)

    def get_job_meta(self, job_id: int) -> Dict[str, str]:
        """Retrieve job metadata."""
        key = meta_key(job_id)
        result = self._execute_with_failover_retry(lambda: self.client.hgetall(key))
        return {
            _text(k):
            _text(v)
            for k, v in result.items()
        }

    def add_compute_host(self, job_id: int, host: dict) -> None:
        """Record a host (utils.metadata.compute_host()) that computed tasks of
        the job. The finalizer copies the set to PostgreSQL."""
        key = hosts_key(job_id)

        def _add():
            pipe = self.client.pipeline()
            pipe.sadd(key, json.dumps(host, sort_keys=True))
            pipe.expire(key, DEFAULT_JOB_TTL_SECONDS)
            pipe.execute()
        self._execute_with_failover_retry(_add)

    def get_compute_hosts(self, job_id: int) -> List[dict]:
        members = self._execute_with_failover_retry(
            lambda: self.client.smembers(hosts_key(job_id)))
        return sorted((json.loads(m) for m in members), key=lambda h: json.dumps(h, sort_keys=True))

    def update_job_meta(self, job_id: int, field: str, value: Any):
        """Update a single metadata field."""
        self._execute_with_failover_retry(
            lambda: self.client.hset(meta_key(job_id), field, str(value))
        )

    # Adaptive Timestep (a_max aggregation)

    def get_epoch_amax_values(self, job_id: int, epoch: int, generation: int) -> List[float]:
        """Read all packages' a_max values for an epoch (deleted by
        cleanup_epoch_data after the merge consumes them)."""
        key = self._epoch_key(job_id, epoch, 'amax', generation)

        def _get():
            return [float(v) for v in self.client.hvals(key)]

        return self._execute_with_failover_retry(_get)

    # Cleanup Operations

    def cleanup_epoch_data(self, job_id: int, epoch: int, generation: int):
        """Remove an epoch's counted markers, a_max values, staging keys and
        enqueue guard after its commit (once the epoch is committed the
        enqueue script refuses it on committed_epoch alone). Nothing is
        deleted unless the epoch is committed in that generation
        (_CLEANUP_EPOCH)."""
        keys = [self._epoch_key(job_id, epoch, suffix, generation)
                for suffix in ('counted', 'amax', 'staging', 'astaging', 'staged', 'enqueued')]
        result = int(self._run_script('cleanup_epoch', _CLEANUP_EPOCH,
                                      [meta_key(job_id)] + keys,
                                      [str(generation), str(epoch)]))
        if result < 0:
            logger.warning(f"Epoch {epoch} of job {job_id} (generation {generation}) is not "
                           f"committed, its keys are kept for a takeover (code {result})")

    def cleanup_job(self, job_id: int):
        """Remove all Redis data for a job: its state and accelerations, and
        every key under job:{job_id}: (meta, epoch keys of every generation,
        leases, markers, hosts, one SCAN covers the epoch keys).
        """
        def _cleanup():
            # the two keys outside the job:{id}: namespace
            self.client.delete(bodies_key(job_id), accelerations_key(job_id))
            # Only the anchored `job:{job_id}:*` pattern is safe: an unanchored
            # glob such as `*:{job_id}:*` also matches other jobs' keys where the
            # number appears as a segment (cleaning job 2 would hit job 12's
            # `...:epoch:2:done`), deleting live coordination state.
            cursor = 0
            while True:
                cursor, keys = self.client.scan(cursor, match=f'{V2_PREFIX}job:{job_id}:*', count=500)
                if keys:
                    self.client.delete(*keys)
                if cursor == 0:
                    break

            logger.info(f"Cleaned up all Redis data for job {job_id}")

        self._execute_with_failover_retry(_cleanup)

    # Lock Operations

    def acquire_finalization_lock(self, job_id: int, ttl_seconds: int = 600) -> Optional[str]:
        """Acquire the lock for job finalization: an owner token, or None if
        another worker holds it. Only one worker uploads the final results at a
        time. The 10-minute TTL covers large uploads."""
        return self._acquire_lock(f'{V2_PREFIX}job:{job_id}:finalize_lock', ttl_seconds)

    def release_finalization_lock(self, job_id: int, token: str) -> None:
        """Release the lock if `token` still holds it: a finalizer whose lock
        expired must not delete the next holder's."""
        self._run_script('release_lease', _RELEASE_LEASE,
                         [f'{V2_PREFIX}job:{job_id}:finalize_lock'], [token])

    @staticmethod
    def _recovery_lock_key(job_id: int) -> str:
        return f'{V2_PREFIX}job:{job_id}:recover_lock'

    def acquire_recovery_lock(self, job_id: int, ttl_seconds: Optional[float] = None) -> Optional[str]:
        """Acquire the job's recovery lock: an owner token, or None while
        another recovery holds it. Held for the whole of recover_job, so two
        recoveries of a job never interleave."""
        return self._acquire_lock(self._recovery_lock_key(job_id), ttl_seconds or RECOVERY_LOCK_TTL_S)

    def renew_recovery_lock(self, job_id: int, token: str, ttl_seconds: Optional[float] = None) -> bool:
        """Extend the recovery lock if `token` still holds it (False: lost)."""
        ttl_ms = int((ttl_seconds or RECOVERY_LOCK_TTL_S) * 1000)
        return bool(self._run_script(
            'renew_lease', _RENEW_LEASE, [self._recovery_lock_key(job_id)], [token, str(ttl_ms)]))

    def release_recovery_lock(self, job_id: int, token: str) -> None:
        """Release the recovery lock if `token` still holds it."""
        self._run_script('release_lease', _RELEASE_LEASE, [self._recovery_lock_key(job_id)], [token])

    def _acquire_lock(self, key: str, ttl_seconds: float) -> Optional[str]:
        token = uuid.uuid4().hex
        acquired = self._execute_with_failover_retry(
            lambda: self.client.set(key, token, nx=True, px=int(ttl_seconds * 1000)))
        if not acquired:
            # A retried SET NX whose first reply was lost finds our own token
            current = self._execute_with_failover_retry(lambda: self.client.get(key))
            acquired = current in (token, token.encode())
        return token if acquired else None

    # Fenced Epoch Commit
    #
    # Registration, commit and enqueue each run as one Lua script (see the
    # module-level sources). Together with the owner-tokened merge lease and the
    # pending-merges set they make the epoch transition safe against duplicate
    # delivery, stale tasks after a recovery, and the death of the merging
    # worker at any point.

    def _script(self, name: str, source: str):
        scripts = self._scripts
        script = scripts.get(name)
        if script is None:
            script = self.client.register_script(source)
            scripts[name] = script
        return script

    def _run_script(self, name: str, source: str, keys: List[str], args: List[Any]):
        """Run a Lua script by SHA, loading it on first use or after a failover
        (redis-py's Script object reloads on NOSCRIPT)."""
        script = self._script(name, source)
        return self._execute_with_failover_retry(
            lambda: script(keys=keys, args=args, client=self.client)
        )

    @staticmethod
    def _epoch_key(job_id: int, epoch: int, suffix: str, generation: int) -> str:
        """Key of one epoch's coordination data. Scoped by generation, so a
        recovery (which bumps it) starts every epoch with fresh counters and
        staging keys: keys of an epoch the old generation had in flight can
        never be counted or committed by the new one."""
        return f'{V2_PREFIX}job:{job_id}:g{generation}:epoch:{epoch}:{suffix}'

    @staticmethod
    def _pending_member(job_id: int, generation: int, epoch: int) -> str:
        return f'{job_id}:{generation}:{epoch}'

    def register_partial(
        self,
        job_id: int,
        generation: int,
        epoch: int,
        package_num: int,
        total_packages: int,
        data: bytes,
        package_size: int,
        a_max: Optional[float] = None,
        accelerations: Optional[bytes] = None,
    ) -> int:
        """Atomically record one package's result for an epoch
        (_REGISTER_PARTIAL): write it (and, for acceleration-predictor
        methods, the accelerations it returned) into its slice of the epoch's
        staging keys, which the commit renames into the state. Record its
        a_max. Mark the package counted (a re-registration counts once). And,
        when the count reaches total_packages, add the epoch to the
        pending-merges set. One atomic step, so no crash window opens between
        marking a package and counting it.

        Returns the number of packages counted, or STALE_GENERATION /
        STALE_EPOCH / JOB_NOT_RUNNING when the task no longer belongs to the
        live epoch (the caller acknowledges it without merging),
        PARTIAL_REJECTED (a result that does not fit its package, nothing
        written) or STATE_MISSING.
        """
        field = f'package_{package_num}'
        keys = [
            meta_key(job_id),
            self._epoch_key(job_id, epoch, 'counted', generation),
            self._epoch_key(job_id, epoch, 'amax', generation),
            PENDING_MERGES_KEY,
            self._epoch_key(job_id, epoch, 'staging', generation),
            self._epoch_key(job_id, epoch, 'astaging', generation),
            self._epoch_key(job_id, epoch, 'staged', generation),
            bodies_key(job_id),
        ]
        # Epoch keys live as long as the job (cleanup deletes them after the
        # merge): with a short TTL, a long gap between two packages (workers
        # scaled down) would expire the count, which then never reaches
        # total_packages, stranding the epoch without a trace.
        args = [
            str(generation), str(epoch), field, data,
            '' if a_max is None else repr(float(a_max)),   # 0.0 is a value too
            str(total_packages), str(DEFAULT_JOB_TTL_SECONDS),
            repr(time.time()), self._pending_member(job_id, generation, epoch),
            accelerations if accelerations else '',
            str(package_num),
            str(int(package_size) * BODY_SIZE),
            str(ACCEL_SIZE), str(BODY_SIZE),
        ]
        return int(self._run_script('register_partial', _REGISTER_PARTIAL, keys, args))

    def _commit_with_wait(self, name: str, source: str, keys, args, job_id: int, epoch: int,
                          committed):
        """Run a commit script and, with Sentinel, WAIT for one replica.

        Replication is asynchronous: a failover right after the commit could
        lose it on the new primary while the merger goes on to enqueue the
        next epoch (whose tasks the fence would then reject). Wait for one
        replica before publishing. A timeout is logged, not fatal
        (min-replicas-to-write already bounds the lag). WAIT covers only the
        writes of its own connection, so the script and the WAIT go out in
        one pipeline, which holds a single pooled connection. `committed(reply)`
        says whether the reply is a commit made now (only then is a missing
        replica acknowledgement worth a warning)."""
        if not self._use_sentinel:
            return self._run_script(name, source, keys, args)
        script = self._script(name, source)

        def _commit_and_wait():
            # EVALSHA sent as a plain pipeline command: redis-py's own script
            # call in a pipeline first checks SCRIPT EXISTS, one more round
            # trip per commit. A primary without the script (first use, or a
            # replica promoted by a failover) answers NOSCRIPT without running
            # anything: load it and send the pipeline again.
            for attempt in range(2):
                pipe = self.client.pipeline(transaction=False)
                pipe.execute_command('EVALSHA', script.sha, len(keys), *keys, *args)
                pipe.execute_command('WAIT', 1, 2000)
                replies = pipe.execute(raise_on_error=False)
                if isinstance(replies[0], redis.exceptions.NoScriptError) and attempt == 0:
                    self.client.script_load(source)
                    continue
                break
            # raise_on_error=False hands errors back as values. Re-raise the
            # failover ones so the retry wrapper reconnects and commits again
            # (safe: the commit is compare-and-set and returns 0 if applied).
            if isinstance(replies[0], (ReadOnlyError, redis.ConnectionError, redis.TimeoutError)):
                raise replies[0]
            return replies

        result, acked = self._execute_with_failover_retry(_commit_and_wait)
        if isinstance(result, Exception):
            raise result
        if committed(result):
            if isinstance(acked, Exception):
                logger.warning(f"WAIT after commit failed: {acked}")
            elif int(acked) < 1:
                logger.warning(f"Commit of job {job_id} epoch {epoch} not yet on a replica")
        return result

    def commit_staged(
        self, job_id: int, generation: int, epoch: int, total_packages: int, next_dt: float,
        sim_time: Optional[float] = None, epoch_dt: Optional[float] = None,
        install_accelerations: bool = False,
    ) -> Tuple[int, bool, int]:
        """Commit an epoch from its staging key (_COMMIT_STAGED): the
        registrations' slices become the job's state by a rename, with a
        compare-and-set of committed_epoch, so exactly one merger commits it.

        install_accelerations: make the returned accelerations staged by the
        registrations the next epoch's a(0) in the same step (when complete).

        Returns (code, a(0) installed, state bytes): code 1 committed now, 0
        already committed, STAGING_INCOMPLETE (nothing written) or a negative
        fence code."""
        keys = [
            meta_key(job_id),
            bodies_key(job_id),
            accelerations_key(job_id),
            self._epoch_key(job_id, epoch, 'publish', generation),
            self._epoch_key(job_id, epoch, 'staging', generation),
            self._epoch_key(job_id, epoch, 'astaging', generation),
            self._epoch_key(job_id, epoch, 'staged', generation),
        ]
        args = [
            str(generation), str(epoch), str(DEFAULT_JOB_TTL_SECONDS),
            repr(float(next_dt)), str(DEFAULT_JOB_TTL_SECONDS),
            '' if sim_time is None else repr(float(sim_time)),
            '' if epoch_dt is None else repr(float(epoch_dt)),
            str(total_packages),
            str(ACCEL_SIZE) if install_accelerations else '',
            str(BODY_SIZE),
        ]
        reply = self._commit_with_wait('commit_staged', _COMMIT_STAGED, keys, args, job_id, epoch,
                                       committed=lambda r: int(r[0]) == 1)
        return int(reply[0]), bool(int(reply[1])), int(reply[2])

    def ship_accelerations(self, job_id: int, generation: int, epoch: int, data: bytes,
                           source: str = '') -> int:
        """Store a(0) of the state committed for `epoch` (fenced, see
        _SHIP_ACCELERATIONS). `source` (returned / exact / fallback) is counted
        in the meta as accel_src_<source>, in the same script, so a run's
        record says which a(0) it used without an extra round trip."""
        keys = [meta_key(job_id), accelerations_key(job_id)]
        args = [str(generation), str(epoch), data, str(DEFAULT_JOB_TTL_SECONDS), source]
        return int(self._run_script('ship_accelerations', _SHIP_ACCELERATIONS, keys, args))

    def get_publish_marker(self, job_id: int, generation: int, epoch: int) -> Optional[float]:
        """Next step size recorded at commit, or None once publication finished."""
        key = self._epoch_key(job_id, epoch, 'publish', generation)
        val = self._execute_with_failover_retry(lambda: self.client.get(key))
        return float(val) if val is not None else None

    def finish_publication(self, job_id: int, generation: int, epoch: int) -> None:
        """Mark an epoch fully published: drop its marker and pending entry."""
        def _finish():
            pipe = self.client.pipeline()
            pipe.delete(self._epoch_key(job_id, epoch, 'publish', generation))
            pipe.zrem(PENDING_MERGES_KEY, self._pending_member(job_id, generation, epoch))
            pipe.execute()

        self._execute_with_failover_retry(_finish)

    def enqueue_epoch_tasks(
        self,
        job_id: int,
        generation: int,
        epoch: int,
        tasks: List[Dict[str, str]],
        epoch_dt: float,
        stream: str = TASKS_STREAM,
        allow_paused: bool = False,
    ) -> int:
        """Enqueue all tasks of one epoch exactly once per generation.

        A guard key makes a replayed publication (a takeover after the original
        merger enqueued) a no-op, and the whole batch is added atomically, so a
        crash can never leave an epoch with only some of its tasks. The same
        script updates current_epoch and the step size of the epoch in flight.
        Returns the number of tasks added, 0 if the epoch was already enqueued, or
        a negative code (STALE_GENERATION, JOB_NOT_RUNNING, JOB_PAUSED)."""
        if not tasks:
            return 0
        field_names = list(tasks[0].keys())
        flat: List[str] = []
        for t in tasks:
            if list(t.keys()) != field_names:
                raise ValueError("All tasks of an epoch must have the same fields")
            for k in field_names:
                flat.extend([k, str(t[k])])
        keys = [
            self._epoch_key(job_id, epoch, 'enqueued', generation),
            meta_key(job_id),
            stream,
        ]
        args = [
            str(generation), str(DEFAULT_JOB_TTL_SECONDS), str(epoch), repr(float(epoch_dt)),
            '1' if allow_paused else '0', str(len(tasks)), str(len(field_names)),
        ] + flat
        return int(self._run_script('enqueue_epoch', _ENQUEUE_EPOCH, keys, args))

    def get_fence_state(self, job_id: int) -> Dict[str, Optional[str]]:
        """generation, committed_epoch and status of a job (None if absent)."""
        vals = self._execute_with_failover_retry(lambda: self.client.hmget(
            meta_key(job_id), 'generation', 'committed_epoch', 'status'
        ))
        dec = [_text(v) for v in vals]
        return {'generation': dec[0], 'committed_epoch': dec[1], 'status': dec[2]}

    def get_task_inputs(self, job_id: int, want_accels: bool = False
                        ) -> Tuple[Dict[str, Optional[str]], Optional[bytes], Optional[bytes]]:
        """A task's reads in one round trip (a non-transactional pipeline, no
        Lua: a script returning the state would copy it into Lua once per
        reader): the fence fields (as get_fence_state), the bodies and, on
        request, the shipped accelerations (else None)."""
        meta = meta_key(job_id)

        def _read():
            pipe = self.client.pipeline(transaction=False)
            pipe.hmget(meta, *_TASK_FENCE_FIELDS)
            pipe.get(bodies_key(job_id))
            if want_accels:
                pipe.get(accelerations_key(job_id))
            return pipe.execute()

        replies = self._execute_with_failover_retry(_read)
        return _task_fence(replies[0]), replies[1], (replies[2] if want_accels else None)

    # State reads from a replica (NBODY_V2_REPLICA_READS)
    #
    # The state broadcast is bandwidth-bound on the primary (1.28 MB to every
    # worker of an epoch). Every commit waits for one replica (WAIT 1), so a
    # replica normally holds the committed state before the next epoch is
    # enqueued. Workers may then read it there. The meta and the state are
    # read together in one MULTI/EXEC on the replica: the commit script writes
    # bodies and committed_epoch in one step, which reaches the replica as one
    # transaction, so a meta saying "generation g, committed e-1" proves the
    # bodies read with it are the state a task of epoch e needs. Anything else
    # (a lagging or newer replica, a missing a(0), an error) returns None and
    # the caller reads from the primary as before. The registration fence
    # stays the final guard. A replica is never used to decide that a task is
    # stale: only the primary's fence may drop a task.

    REPLICA_RETRY_AFTER_S = 10.0

    def _replica(self):
        """A client for a replica (Sentinel slave_for, created once), or None
        when replica reads are off, there is no Sentinel, or the replica
        failed less than REPLICA_RETRY_AFTER_S ago. Tests may set
        replica_client directly."""
        if not cfg.replica_reads:
            return None
        if time.monotonic() < getattr(self, '_replica_down_until', 0.0):
            return None
        client = getattr(self, 'replica_client', None)
        if client is None and self._use_sentinel and self.sentinel is not None:
            client = self.sentinel.slave_for(
                cfg.sentinel_master, password=cfg.password, db=cfg.db,
                decode_responses=False, socket_timeout=cfg.socket_timeout,
                socket_connect_timeout=cfg.socket_connect_timeout, socket_keepalive=True,
            )
            self.replica_client = client
        if client is not None:
            _count_round_trips(client)
        return client

    def get_task_inputs_from_replica(self, job_id: int, generation: int, epoch: int,
                                     want_accels: bool = False):
        """A task's inputs (as get_task_inputs) read from a replica in one
        round trip, or None if they cannot be proved to be the state the task
        of `epoch` in `generation` needs (then read from the primary)."""
        client = self._replica()
        if client is None:
            return None
        meta = meta_key(job_id)
        try:
            pipe = client.pipeline(transaction=True)
            pipe.hmget(meta, *_TASK_FENCE_FIELDS)
            pipe.get(bodies_key(job_id))
            if want_accels:
                pipe.get(accelerations_key(job_id))
            replies = pipe.execute()
        except Exception as e:  # noqa: BLE001 — discovery or connection error: use the primary
            self._replica_down_until = time.monotonic() + self.REPLICA_RETRY_AFTER_S
            logger.warning(f"Replica read failed ({e}), reading from the primary for "
                           f"{self.REPLICA_RETRY_AFTER_S:.0f} s")
            return None
        fields = _task_fence(replies[0])
        fence = FenceState.of(fields)
        bodies = replies[1]
        accels = replies[2] if want_accels else None
        if (not fence.is_current(generation) or not fence.committed_is(epoch - 1)
                or fence.status not in ('IN_PROGRESS', 'PAUSED') or bodies is None
                or (want_accels and accels is None)):
            return None
        return fields, bodies, accels

    def mark_started(self, job_id: int) -> bool:
        """Record the job's start time once. True only for the first caller.
        Never creates the meta: a plain HSETNX on a meta that was cleaned up or
        expired meanwhile recreated it without status or TTL."""
        return bool(self._run_script('mark_started', _MARK_STARTED,
                                     [meta_key(job_id)], [repr(time.time())]))

    @staticmethod
    def fresh_generation_seed() -> int:
        """A generation no earlier incarnation of a job can have used, for a
        meta rebuilt without one (lost from Redis, rebuilt from PostgreSQL):
        restarting at 0 would reuse generation numbers whose tasks, keys and
        pending merges may still be around. The wall clock in milliseconds
        grows across rebuilds as long as fewer recoveries than milliseconds
        separate two of them, and stays far below 2^53 (exact in Lua numbers)."""
        return int(time.time() * 1000)

    def begin_recovery(self, job_id: int, lock_token: str) -> int:
        """Bump the generation and set the job IN_PROGRESS, fenced on the
        recovery lock and on the job not being cancelled (see _BEGIN_RECOVERY).
        Returns the new generation, or a negative code."""
        return int(self._run_script(
            'begin_recovery', _BEGIN_RECOVERY,
            self._job_keys(job_id) + [self._recovery_lock_key(job_id)],
            [lock_token, str(DEFAULT_JOB_TTL_SECONDS)],
        ))

    def restore_recovered_state(self, job_id: int, generation: int, state: bytes,
                                accelerations: Optional[bytes], epoch: int, next_dt: float,
                                sim_time: Optional[float], committed_dt: Optional[float] = None) -> int:
        """Write a recovery's body state, a(0) (None: workers compute it) and
        fencing fields in one step, fenced on its generation (see
        _RESTORE_STATE). Returns 1 or a negative code (nothing written)."""
        return int(self._run_script(
            'restore_state', _RESTORE_STATE, self._job_keys(job_id),
            [str(generation), str(DEFAULT_JOB_TTL_SECONDS), state, accelerations or b'', str(epoch),
             repr(float(next_dt)), '' if sim_time is None else repr(float(sim_time)),
             '' if committed_dt is None else repr(float(committed_dt))],
        ))

    def _job_keys(self, job_id: int) -> List[str]:
        return [meta_key(job_id), bodies_key(job_id),
                accelerations_key(job_id)]

    def fail_job(self, job_id: int, reason: str, generation: Optional[int] = None,
                 committed_epoch: Optional[int] = None) -> int:
        """Mark a running job FAILED (fenced, see _FAIL_JOB). Returns 1 if it
        was marked, or a negative code when the caller is stale (generation,
        or the committed epoch it observed) or the job is no longer running."""
        return int(self._run_script(
            'fail_job', _FAIL_JOB, self._job_keys(job_id),
            ['' if generation is None else str(generation), reason[:500], str(DEFAULT_JOB_TTL_SECONDS),
             '' if committed_epoch is None else str(committed_epoch)],
        ))

    def finalize_job_meta(self, job_id: int, generation: int, final_epoch: int, results_key: str) -> int:
        """Mark a job COMPLETED (fenced, see _FINALIZE_JOB)."""
        return int(self._run_script(
            'finalize_job', _FINALIZE_JOB, self._job_keys(job_id),
            [str(generation), str(final_epoch), results_key, str(DEFAULT_JOB_TTL_SECONDS)],
        ))

    def pause_job(self, job_id: int) -> int:
        """Pause a running job and keep its meta, state and a(0) without a TTL
        (see _PAUSE_JOB). Returns 1, 0 (already paused) or a negative code."""
        return int(self._run_script('pause_job', _PAUSE_JOB, self._job_keys(job_id), []))

    def resume_job(self, job_id: int) -> int:
        """Flip a resumable PAUSED job to IN_PROGRESS and restore its TTLs
        (see _RESUME_JOB). Returns 1 or a negative code."""
        return int(self._run_script('resume_job', _RESUME_JOB, self._job_keys(job_id),
                                    [str(DEFAULT_JOB_TTL_SECONDS)]))

    def cancel_job(self, job_id: int) -> int:
        """Cancel a job not yet in a final state (see _CANCEL_JOB)."""
        return int(self._run_script('cancel_job', _CANCEL_JOB, self._job_keys(job_id),
                                    [str(DEFAULT_JOB_TTL_SECONDS)]))

    # instrumentation (task_timing)

    def push_timing(self, job_id: int, record: Dict[str, Any], ttl_seconds: int = 7 * 24 * 3600) -> None:
        """Append one timing record to v2:timings:{job_id} (kept after job cleanup)."""
        key = f'{V2_PREFIX}timings:{job_id}'

        def _push():
            pipe = self.client.pipeline()
            pipe.rpush(key, json.dumps(record, separators=(',', ':')))
            pipe.expire(key, ttl_seconds)
            pipe.execute()

        self._execute_with_failover_retry(_push)

    def store_diagnostics(self, job_id: int, epoch: int, values: Dict[str, Any],
                          ttl_seconds: int = 30 * 24 * 3600) -> None:
        """Record conserved quantities of the state after `epoch` in v2:diag:{job_id}."""
        key = f'{V2_PREFIX}diag:{job_id}'

        def _store():
            pipe = self.client.pipeline()
            pipe.hset(key, str(epoch), json.dumps(values, separators=(',', ':')))
            pipe.expire(key, ttl_seconds)
            pipe.execute()

        self._execute_with_failover_retry(_store)

    # merge lease

    @staticmethod
    def _lease_key(job_id: int, generation: int, epoch: int) -> str:
        return f'{V2_PREFIX}job:{job_id}:merge_lease:g{generation}:{epoch}'

    def renew_merge_lease(
        self, job_id: int, generation: int, epoch: int, token: str,
        lease_ms: int = MERGE_LEASE_MS,
    ) -> bool:
        """Extend the lease if this token still owns it."""
        return bool(self._run_script(
            'renew_lease', _RENEW_LEASE,
            [self._lease_key(job_id, generation, epoch)], [token, str(lease_ms)],
        ))

    def release_merge_lease(self, job_id: int, generation: int, epoch: int, token: str) -> bool:
        """Release the lease only if this token still owns it."""
        return bool(self._run_script(
            'release_lease', _RELEASE_LEASE,
            [self._lease_key(job_id, generation, epoch)], [token],
        ))

    def begin_merge(
        self, job_id: int, generation: int, epoch: int, owner: str,
        lease_ms: int = MERGE_LEASE_MS, want_amax: bool = False,
    ) -> Optional[MergeStart]:
        """Take the merge lease of an epoch and read what its commit needs, in
        one round trip (_BEGIN_MERGE): the fence, the previous simulation time
        and, on request and if the epoch is still to be committed in the live
        generation, its a_max values. None if another live worker holds the
        lease."""
        token = f'{owner}:{uuid.uuid4().hex}'
        keys = [self._lease_key(job_id, generation, epoch), meta_key(job_id)]
        args = [token, str(int(lease_ms))]
        script = self._script('begin_merge', _BEGIN_MERGE)

        def _begin():
            # EVALSHA as a plain command (no SCRIPT EXISTS round trip). A
            # primary without the script answers NOSCRIPT for it alone (the
            # reads still run): load it and send the transaction again.
            for attempt in range(2):
                pipe = self.client.pipeline(transaction=True)
                pipe.execute_command('EVALSHA', script.sha, len(keys), *keys, *args)
                if want_amax:
                    pipe.hvals(self._epoch_key(job_id, epoch, 'amax', generation))
                replies = pipe.execute(raise_on_error=False)
                if isinstance(replies[0], redis.exceptions.NoScriptError) and attempt == 0:
                    self.client.script_load(_BEGIN_MERGE)
                    continue
                break
            for r in replies:
                if isinstance(r, (ReadOnlyError, redis.ConnectionError, redis.TimeoutError)):
                    raise r
            for r in replies:
                if isinstance(r, Exception):
                    raise r
            return replies

        replies = self._execute_with_failover_retry(_begin)
        reply = replies[0]
        if not reply or int(reply[0]) != 1:
            return None
        amax_raw = replies[1] if want_amax else []

        gen, committed, status, sim_time = (_text(v) for v in reply[1:5])
        fence = FenceState(gen, committed, status)
        fetched = (fence.is_current(generation)
                   and (fence.committed is None or fence.committed < epoch))
        return MergeStart(
            token=token, fence=fence, sim_time=sim_time,
            amax=[float(v) for v in amax_raw] if fetched and want_amax else None,
        )

    def publish_epoch(
        self, job_id: int, generation: int, epoch: int, token: str, n_tasks: int,
        n_bodies: int, package_size: int, template: List[str], next_epoch_dt: float,
        time_step: str, stream: str = TASKS_STREAM,
    ) -> Tuple[int, int, bool]:
        """Publish a committed, non-final epoch in one round trip
        (_PUBLISH_EPOCH): clean up its keys, enqueue the next epoch, finish the
        publication and release the merge lease (token). The next epoch's
        tasks are built in the script from the job-level template
        (epoch_tasks.epoch_task_template) and the package bounds. Returns
        (cleanup code, enqueue code, published). Published False only when the
        commit was lost, in which case nothing was changed (the epoch stays
        pending, the lease stays with its holder)."""
        if n_tasks < 1 or len(template) % 2:
            raise ValueError("publish_epoch needs tasks and field-value pairs")
        keys = [meta_key(job_id)]
        keys += [self._epoch_key(job_id, epoch, suffix, generation)
                 for suffix in ('counted', 'amax', 'enqueued', 'staging', 'astaging', 'staged')]
        keys += [
            self._epoch_key(job_id, epoch + 1, 'enqueued', generation),
            stream,
            self._epoch_key(job_id, epoch, 'publish', generation),
            PENDING_MERGES_KEY,
            self._lease_key(job_id, generation, epoch),
        ]
        args = [
            str(generation), str(epoch), token, self._pending_member(job_id, generation, epoch),
            str(DEFAULT_JOB_TTL_SECONDS), str(epoch + 1), repr(float(next_epoch_dt)),
            str(int(n_tasks)), str(int(n_bodies)), str(int(package_size)), str(job_id),
            str(len(template) // 2),
        ] + [str(x) for x in template] + [time_step]
        cleanup, enqueued, published = self._run_script('publish_epoch', _PUBLISH_EPOCH, keys, args)
        return int(cleanup), int(enqueued), bool(int(published))

    def merge_lease_owner(self, job_id: int, generation: int, epoch: int) -> Optional[str]:
        key = self._lease_key(job_id, generation, epoch)
        val = self._execute_with_failover_retry(lambda: self.client.get(key))
        return _text(val)

    # pending merges (takeover)

    def get_pending_merges(self, older_than_s: float = 0.0, limit: int = 10) -> List[Tuple[int, int, int]]:
        """Epochs that became mergeable at least older_than_s ago and have not
        finished publishing, as (job_id, generation, epoch)."""
        cutoff = time.time() - older_than_s
        members = self._execute_with_failover_retry(lambda: self.client.zrangebyscore(
            PENDING_MERGES_KEY, '-inf', cutoff, start=0, num=limit))
        out = []
        for m in members:
            s = _text(m)
            try:
                job, gen, epoch = (int(x) for x in s.split(':'))
                out.append((job, gen, epoch))
            except ValueError:
                self._execute_with_failover_retry(lambda m=m: self.client.zrem(PENDING_MERGES_KEY, m))
        return out

    def add_pending_merge(self, job_id: int, generation: int, epoch: int, delay_s: float = 0.0) -> None:
        """Mark an epoch as needing its publication finished by a takeover,
        taken up no sooner than delay_s from now (plus the takeover grace)."""
        self._execute_with_failover_retry(
            lambda: self.client.zadd(PENDING_MERGES_KEY,
                                     {self._pending_member(job_id, generation, epoch): time.time() + delay_s})
        )

    def requeue_pending_merge(self, job_id: int, generation: int, epoch: int) -> None:
        """Move an existing pending entry to the back (retry after the others)."""
        self._execute_with_failover_retry(
            lambda: self.client.zadd(PENDING_MERGES_KEY,
                                     {self._pending_member(job_id, generation, epoch): time.time()}, xx=True)
        )

    def drop_pending_merge(self, job_id: int, generation: int, epoch: int) -> None:
        self._execute_with_failover_retry(
            lambda: self.client.zrem(PENDING_MERGES_KEY, self._pending_member(job_id, generation, epoch))
        )

    # Stream Maintenance

    def prune_task_stream(self, stream: str = TASKS_STREAM) -> None:
        """Drop the task-stream entries no consumer group can still need.

        XACK removes an entry from a group's pending list but not from the
        stream, so without trimming the stream grows one entry per task. An
        entry is kept while any group has not yet delivered it, or holds it
        pending (a worker on it, or a dead worker's, to be reclaimed): the trim
        is XTRIM MINID at the oldest such entry. (A length cap such as XTRIM
        MAXLEN would ignore both: behind a large enough backlog it deletes tasks
        never delivered or still to be reclaimed.)
        """
        def _prune():
            try:
                floor = self._task_stream_floor(stream)
                if floor is not None:
                    # approximate: removes only whole radix-tree nodes below the
                    # floor, so it may keep a little more, never less
                    self.client.xtrim(stream, minid=floor, approximate=True)
                    logger.debug(f"Pruned task stream {stream} below {floor}")
            except redis.ResponseError as e:
                logger.warning(f"Failed to prune task stream: {e}")

        self._execute_with_failover_retry(_prune)

    def _task_stream_floor(self, stream: str) -> Optional[str]:
        """The oldest entry ID any consumer group of the stream still needs (its
        oldest pending entry, else its last delivered one), or None to keep all."""
        def sid(x):
            x = x.decode() if isinstance(x, bytes) else str(x)
            ms, _, seq = x.partition('-')
            return int(ms), int(seq or 0)

        floor = None
        for group in self.client.xinfo_groups(stream):
            name = group['name']
            needed = sid(group['last-delivered-id'])
            pending = self.client.xpending(stream, name)
            if pending and pending.get('pending'):
                needed = min(needed, sid(pending['min']))
            floor = needed if floor is None else min(floor, needed)
        if floor is None or floor == (0, 0):
            return None
        return f"{floor[0]}-{floor[1]}"

    def cleanup_idle_consumers(self, min_idle_ms: int = 3600000, stream: str = TASKS_STREAM) -> int:
        """Remove consumers idle for longer than min_idle_ms that own no pending
        messages (XGROUP DELCONSUMER would drop their pending entries, losing
        tasks). Worker names are per process, so every pod restart adds one.
        Returns how many were removed."""
        def _cleanup():
            removed = 0
            try:
                for consumer in self.client.xinfo_consumers(stream, CONSUMER_GROUP):
                    name = consumer.get('name', b'')
                    name = _text(name)
                    if consumer.get('idle', 0) > min_idle_ms and consumer.get('pending', 0) == 0:
                        self.client.xgroup_delconsumer(stream, CONSUMER_GROUP, name)
                        removed += 1
                        logger.info(f"Removed idle consumer: {name}")
            except redis.ResponseError as e:
                logger.warning(f"Failed to cleanup consumers: {e}")
            return removed

        return self._execute_with_failover_retry(_cleanup)

    def delete_other_generation_keys(self, job_id: int, keep: int) -> int:
        """Delete the epoch keys of every generation of a job but `keep` (after
        a recovery that rebuilt the meta and so no longer knows the previous
        generation). Fenced on
        `keep` being the live generation, per batch (_DELETE_FOR_GENERATION):
        returns the number of keys deleted, or STALE_GENERATION once a later
        recovery has taken over (its keys are left alone)."""
        prefix = f'{V2_PREFIX}job:{job_id}:g{keep}:'.encode()
        meta = meta_key(job_id)

        def _delete_batch(keys):
            return int(self._run_script('delete_for_generation', _DELETE_FOR_GENERATION,
                                        [meta] + keys, [str(keep)]))

        def _scan():
            # the SCAN is restarted from scratch if a failover interrupts it
            batches, cursor = [], 0
            while True:
                cursor, keys = self.client.scan(cursor, match=f'{V2_PREFIX}job:{job_id}:g*', count=200)
                batches.append([k for k in keys if not k.startswith(prefix)])
                if cursor == 0:
                    return batches

        batches = self._execute_with_failover_retry(_scan)
        n = 0
        for keys in batches:
            deleted = _delete_batch(keys)
            if deleted < 0:
                return deleted
            n += deleted
        return n

    # Utility Methods

    def get_pending_info(self) -> Dict[str, Any]:
        """Pending tasks of the CPU and the GPU task stream (for monitoring):
        the total, the count per stream, and the consumers holding them."""
        total = 0
        per_stream = {}
        consumers: Dict[str, int] = {}
        for stream in (TASKS_STREAM, GPU_TASKS_STREAM):
            try:
                result = self.client.xpending(stream, CONSUMER_GROUP)
            except redis.ResponseError:      # stream or group not created yet
                result = None
            count = result['pending'] if result else 0
            per_stream[stream] = count
            total += count
            for c in (result or {}).get('consumers', []) or []:
                name = _text(c['name'])
                consumers[name] = consumers.get(name, 0) + int(c['pending'])
        return {
            'pending_count': total,
            'streams': per_stream,
            'consumers': [{'name': n, 'pending': p} for n, p in consumers.items()],
        }


# Global instance (created on first import)
_redis_service: Optional[RedisService] = None


def get_redis_service() -> RedisService:
    """Get or create the global Redis service instance."""
    global _redis_service
    if _redis_service is None:
        _redis_service = RedisService()
    return _redis_service
