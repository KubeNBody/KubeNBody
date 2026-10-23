# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Process-wide fault-injection state shared by the worker's threads.

FROZEN is set while a `freeze` fault (NBODY_FAULT_INJECT "...@freeze=S") holds
the worker: the main thread sleeps, and the merge-lease renewer and the task
heartbeat skip their renewals, so the process behaves like one stopped by
SIGSTOP (a zombie): its lease and its task expire, another worker takes over,
and when it resumes every write it attempts must be rejected or idempotent.
"""
import threading


FROZEN = threading.Event()
