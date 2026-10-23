# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add indexes for job_type and force_algorithm fields.

These indexes improve query performance when filtering jobs by type or algorithm.

Date: 2025-01-15
Related: Job listing performance optimization
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add indexes for job_type and force_algorithm."""

    # Create indexes for common filter operations
    migrator.sql('CREATE INDEX IF NOT EXISTS idx_job_job_type ON job(job_type)')
    migrator.sql('CREATE INDEX IF NOT EXISTS idx_job_force_algorithm ON job(force_algorithm)')


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove indexes."""

    migrator.sql('DROP INDEX IF EXISTS idx_job_job_type')
    migrator.sql('DROP INDEX IF EXISTS idx_job_force_algorithm')
