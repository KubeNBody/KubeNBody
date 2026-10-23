# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Reproducibility metadata collection for simulation runs."""
import os
import platform
import subprocess
import sys

import numba
import numpy


def cpu_model() -> str:
    """The CPU model name ('model name' in /proc/cpuinfo on Linux, where
    platform.processor() is usually empty), or the best the platform offers.

    Recorded per run because the force kernels are compiled with fastmath for
    the host CPU: bitwise comparisons between runs hold for the same model."""
    try:
        with open('/proc/cpuinfo') as f:
            for line in f:
                if line.lower().startswith(('model name', 'cpu model')):
                    return line.split(':', 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def compute_host() -> dict:
    """Where a job is computed: CPU model, logical CPUs visible, and the
    Kubernetes node (NODE_NAME from the downward API) or the host name."""
    return {"cpu_model": cpu_model(), "cpu_count": os.cpu_count(),
            "node": os.environ.get("NODE_NAME") or platform.node()}


def collect_metadata() -> dict:
    """Collect reproducibility metadata for a simulation run. Called by the API
    at submission, so the host fields describe the API pod. The workers that
    compute the job add their own under "compute_hosts" (compute_host())."""
    meta = {
        "python_version": sys.version,
        "platform": platform.platform(),
        "cpu": cpu_model(),
        "cpu_count": os.cpu_count(),
    }

    meta["numpy_version"] = numpy.__version__
    meta["numba_version"] = numba.__version__

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            meta["git_sha"] = result.stdout.strip()
    except Exception:
        pass

    return meta
