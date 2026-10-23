# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add GPU support fields to job table.

Adds tracking for GPU-enabled jobs and device assignment.

Date: 2025-01-12
Related: GPU N-body implementation
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add GPU support fields."""

    # Add GPU enabled flag (FALSE for the existing rows)
    migrator.sql('ALTER TABLE job ADD COLUMN gpu_enabled BOOLEAN DEFAULT FALSE')

    # Add GPU device ID (NULL = not assigned or N/A)
    migrator.sql('ALTER TABLE job ADD COLUMN gpu_device_id INTEGER NULL')

    # Backfill existing jobs as non-GPU
    migrator.sql("UPDATE job SET gpu_enabled = FALSE WHERE gpu_enabled IS NULL")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove GPU support fields."""

    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS gpu_enabled')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS gpu_device_id')
