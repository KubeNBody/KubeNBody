# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Entry point of the monolithic GPU worker: whole jobs (all epochs) on the
PyCUDA backend, taken from RabbitMQ."""

from rabbitmq import GPU_MONOLITHIC_QUEUE
from workers import MonolithicWorker
from workers.backends import GPUBackend
from workers.entry import run_worker


def main():
    run_worker("Monolithic GPU worker", 'worker-monolithic-gpu', 'monolithic', 'gpu', GPU_MONOLITHIC_QUEUE,
               lambda: MonolithicWorker(compute_backend=GPUBackend()))


if __name__ == '__main__':
    main()
