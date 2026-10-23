# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Canonical enum definitions for the application.

All status enums are (str, Enum) so they work with both Pydantic models
and plain Python comparisons without needing .value access.
"""
from enum import Enum


class JobStatus(str, Enum):
    """Status states for Job model."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PackageStatus(str, Enum):
    """Status states for Package model."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    TO_MERGE = "TO_MERGE"
    MERGED = "MERGED"
    FAILED = "FAILED"


class JobType(str, Enum):
    """Job execution type."""
    DISTRIBUTED = "distributed"
    MONOLITHIC = "monolithic"
    DISTRIBUTED_V2 = "distributed_v2"
