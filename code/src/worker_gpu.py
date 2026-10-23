# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Entry point of the V1 distributed GPU worker: DistributedWorkerV1 with the
PyCUDA backend, taking its packages from RabbitMQ."""

from file_service import ensure_buckets_exist
from rabbitmq import GPU_DISTRIBUTED_QUEUE
from workers import DistributedWorkerV1
from workers.backends import GPUBackend, RabbitMQBackend
from workers.entry import run_worker


def main():
    run_worker("V1 distributed GPU worker", 'worker-gpu', 'distributed', 'gpu', GPU_DISTRIBUTED_QUEUE,
               lambda: DistributedWorkerV1(compute_backend=GPUBackend(), message_backend=RabbitMQBackend()),
               prepare=ensure_buckets_exist)


if __name__ == '__main__':
    main()
