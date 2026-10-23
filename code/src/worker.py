# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Entry point of the V1 distributed CPU worker: DistributedWorkerV1 with the
Numba CPU backend, taking its packages from RabbitMQ."""

from file_service import ensure_buckets_exist
from rabbitmq import NEW_QUEUE
from workers import DistributedWorkerV1
from workers.backends import CPUBackend, RabbitMQBackend
from workers.entry import run_worker


def main():
    run_worker("V1 distributed CPU worker", 'worker', 'distributed', 'cpu', NEW_QUEUE,
               lambda: DistributedWorkerV1(compute_backend=CPUBackend(), message_backend=RabbitMQBackend()),
               prepare=ensure_buckets_exist)


if __name__ == '__main__':
    main()
