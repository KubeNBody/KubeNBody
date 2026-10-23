# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Entry point of the V2 distributed CPU worker: DistributedWorkerV2 with the
Numba CPU backend, taking its tasks from Redis Streams."""

from v2.file_service_v2 import EPOCH_RESULTS_BUCKET, ensure_bucket_exists
from workers import DistributedWorkerV2
from workers.backends import CPUBackend, RedisBackend
from workers.entry import run_worker


def main():
    # queue_name is ignored by the Redis backend (it reads its own stream)
    run_worker("V2 distributed CPU worker (Redis)", 'worker-v2', 'distributed_v2', 'cpu', 'tasks',
               lambda: DistributedWorkerV2(compute_backend=CPUBackend(), message_backend=RedisBackend()),
               prepare=lambda: ensure_bucket_exists(EPOCH_RESULTS_BUCKET))


if __name__ == '__main__':
    main()
