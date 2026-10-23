# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Entry point of the monolithic CPU worker: whole jobs (all epochs) on the
Numba CPU backend, taken from RabbitMQ."""

from rabbitmq import MONOLITHIC_QUEUE
from workers import MonolithicWorker
from workers.backends import CPUBackend
from workers.entry import run_worker


def main():
    run_worker("Monolithic CPU worker", 'worker-monolithic', 'monolithic', 'cpu', MONOLITHIC_QUEUE,
               lambda: MonolithicWorker(compute_backend=CPUBackend()))


if __name__ == '__main__':
    main()
