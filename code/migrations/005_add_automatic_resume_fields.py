# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add automatic resume system fields to job table.

Adds retry tracking, worker lease system, and input bucket storage
to support automatic job resume on crash/restart.

Date: 2025-01-11
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add automatic resume system fields and indexes."""

    # Add retry tracking fields (survives watchdog restarts)
    migrator.sql('ALTER TABLE job ADD COLUMN retry_count INTEGER DEFAULT 0')
    migrator.sql('ALTER TABLE job ADD COLUMN last_retry_at TIMESTAMP WITH TIME ZONE NULL')

    # Add worker lease system fields (prevents duplicate processing)
    migrator.sql('ALTER TABLE job ADD COLUMN worker_id VARCHAR(255) NULL')
    migrator.sql('ALTER TABLE job ADD COLUMN leased_at TIMESTAMP WITH TIME ZONE NULL')

    # Add bucket storage (for watchdog republishing)
    migrator.sql("ALTER TABLE job ADD COLUMN input_bucket VARCHAR(255) NOT NULL DEFAULT 'inputfiles'")

    # Backfill input_bucket for existing jobs (ensures consistency)
    migrator.sql("UPDATE job SET input_bucket = 'inputfiles' WHERE input_bucket IS NULL")

    # Add partial indexes for performance
    # idx_job_lease: Optimizes lease acquisition queries (WHERE worker_id IS NOT NULL)
    migrator.sql('CREATE INDEX idx_job_lease ON job(worker_id, leased_at) WHERE worker_id IS NOT NULL')

    # idx_job_stale: Optimizes watchdog stale job detection (WHERE status = 'IN_PROGRESS')
    migrator.sql("CREATE INDEX idx_job_stale ON job(status, job_type, updated_at) WHERE status = 'IN_PROGRESS'")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove automatic resume system fields and indexes."""

    # Drop indexes first (required before dropping columns)
    migrator.sql('DROP INDEX IF EXISTS idx_job_lease')
    migrator.sql('DROP INDEX IF EXISTS idx_job_stale')

    # Drop columns
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS retry_count')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS last_retry_at')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS worker_id')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS leased_at')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS input_bucket')
