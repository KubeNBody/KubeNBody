# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Workers: distributed (v1 RabbitMQ, v2 Redis Streams) and monolithic, each
with a CPU or GPU compute backend, e.g.
DistributedWorkerV2(CPUBackend(), RedisBackend()).run('tasks').

The worker classes are loaded on first access (PEP 562 __getattr__): their
modules pull in models, the S3 clients and pandas, which code that needs only
the backends should not.
"""

import importlib

from workers.base.compute_backend import ComputeBackend, ComputeError
from workers.base.message_backend import MessageBackend, MessageBackendError, TaskMessage


# Lazy submodule map: attribute name -> (module path, attribute in module).
# Resolved by __getattr__ below on first access.
_LAZY_ATTRS = {
    'DistributedWorker': ('workers.distributed_worker', 'DistributedWorker'),
    'DistributedWorkerV1': ('workers.distributed_worker', 'DistributedWorkerV1'),
    'DistributedWorkerV2': ('workers.distributed_worker_v2', 'DistributedWorkerV2'),
    'MonolithicWorker': ('workers.monolithic_worker', 'MonolithicWorker'),
    'MonolithicJobConfig': ('workers.monolithic_worker', 'MonolithicJobConfig'),
}


def __getattr__(name):  # PEP 562
    """Load a worker class on first access (see the module docstring)."""
    if name in _LAZY_ATTRS:
        module_path, attr_name = _LAZY_ATTRS[name]
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        # Cache on the package so subsequent accesses skip __getattr__.
        globals()[name] = value
        return value
    raise AttributeError(f"module 'workers' has no attribute {name!r}")


def __dir__():
    """Include the lazily loaded names in dir()."""
    return sorted(list(globals().keys()) + list(_LAZY_ATTRS.keys()))


__all__ = [
    # Base classes (eagerly available)
    'ComputeBackend',
    'ComputeError',
    'MessageBackend',
    'TaskMessage',
    'MessageBackendError',
    # Unified workers (lazy)
    'DistributedWorker',
    'DistributedWorkerV1',
    'DistributedWorkerV2',
    'MonolithicWorker',
    'MonolithicJobConfig',
]
