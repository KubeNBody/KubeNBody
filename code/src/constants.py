# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Application-wide constants."""

# File Validation Settings
MAX_BODIES_PER_FILE = 1_000_000  # Prevent OOM from huge uploads
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # Uploaded input files (v1 and v2 submissions)
MAX_EPOCHS = 2**31 - 1  # Job.num_epochs is a PostgreSQL INTEGER

# RabbitMQ Settings
RABBITMQ_RECONNECT_DELAY_SECONDS = 5

# Worker Settings
WORKER_PREFETCH_COUNT = 1

# Merge Settings
MERGE_DOWNLOAD_TIMEOUT_SECONDS = 300

# Physics Constants
# NOTE: the cache=True numba kernels (physics.py, barnes_hut.py) read G_CONST,
# SOFTENING_SQ and the FR_* coefficients below as globals, frozen into the
# compiled code. numba's on-disk cache is keyed on the kernel's own source file,
# not on this one: after changing any of these values, clear the numba cache
# (the __pycache__/*.nbi/*.nbc files, or NUMBA_CACHE_DIR) or stale values are used.
G_CONST = 1.0  # Gravitational constant (normalized, a float, so numba compiles one signature)

# Forest-Ruth (Yoshida 4th-order) composition coefficients, shared by every
# Forest-Ruth stepper (CPU direct and Barnes-Hut, monolithic and package, GPU
# host code). Drift-kick sequence, each coefficient times dt:
#   D(FR_C1) K(FR_D1) D(FR_C2) K(FR_D2) D(FR_C2) K(FR_D1) D(FR_C1)
_FR_W1 = 1.0 / (2.0 - 2.0 ** (1.0 / 3.0))   # chi ≈ 1.3512
_FR_W0 = 1.0 - 2.0 * _FR_W1
FR_C1 = _FR_W1 * 0.5             # drifts 1 and 4
FR_C2 = (_FR_W1 + _FR_W0) * 0.5  # drifts 2 and 3
FR_D1 = _FR_W1                   # kicks 1 and 3
FR_D2 = _FR_W0                   # kick 2
# Cumulative drift time (times dt) reached before each kick: the stage time the
# package steppers predict the background bodies to.
FR_TAU1 = FR_C1
FR_TAU2 = FR_C1 + FR_C2
FR_TAU3 = FR_C1 + 2.0 * FR_C2

# Barnes-Hut Algorithm Settings
DEFAULT_THETA = 0.5  # Default opening angle for Barnes-Hut (balanced accuracy/speed)
MAX_TREE_DEPTH = 50  # Maximum octree depth
SOFTENING_SQ = 1e-9  # Softening squared to prevent singularities (numba-cache note at G_CONST)

# Longest job name (Job.name): a short title, not a note (that is the description)
JOB_NAME_MAX = 120

# Monolithic Worker Lease Settings
# A monolithic job's lease, renewed from a background thread (LeaseRenewer)
# every LEASE_RENEWAL_INTERVAL_SECONDS, and every LEASE_RETRY_AFTER_FAILURE_S
# after a failed renewal (database down) until one succeeds. A lease not
# renewed for LEASE_DURATION_SECONDS is a dead owner's: the watchdog
# republishes the job once it is also past its grace (watchdog.py), and the
# job resumes from its last snapshot. 300 s outlasts a database outage of
# about four minutes without a takeover. A false takeover costs recomputation
# since the last snapshot, never a wrong result (the old owner stops).
LEASE_DURATION_SECONDS = 300
LEASE_RENEWAL_INTERVAL_SECONDS = 60
LEASE_RETRY_AFTER_FAILURE_S = 5
MAX_REPUBLISH_ATTEMPTS = 3  # Max retries before marking job as FAILED
PROGRESS_LOG_INTERVAL = 100  # Log progress and update DB every N epochs
