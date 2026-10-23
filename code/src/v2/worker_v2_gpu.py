# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Entry point of the V2 distributed GPU worker: DistributedWorkerV2 with the
PyCUDA backend, taking its tasks from the GPU Redis stream."""

from v2.file_service_v2 import EPOCH_RESULTS_BUCKET, ensure_bucket_exists
from v2.redis_service import GPU_TASKS_STREAM
from workers import DistributedWorkerV2
from workers.backends import GPUBackend, RedisBackend
from workers.entry import run_worker


def main():
    run_worker(f"V2 distributed GPU worker (Redis stream {GPU_TASKS_STREAM})", 'worker-v2-gpu',
               'distributed_v2', 'gpu', 'gpu_tasks',
               lambda: DistributedWorkerV2(compute_backend=GPUBackend(),
                                           message_backend=RedisBackend(stream=GPU_TASKS_STREAM)),
               prepare=lambda: ensure_bucket_exists(EPOCH_RESULTS_BUCKET))


if __name__ == '__main__':
    main()
