# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add force algorithm selection fields to job table.

Adds support for Barnes-Hut algorithm selection:
- force_algorithm: 'direct' (O(N²)) or 'barnes_hut' (O(N log N))
- barnes_hut_theta: Opening angle parameter for Barnes-Hut

Date: 2025-01-15
Related: Barnes-Hut algorithm implementation
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add algorithm selection fields."""

    # Add force_algorithm field ('direct' for the existing rows)
    migrator.sql("ALTER TABLE job ADD COLUMN force_algorithm VARCHAR(20) DEFAULT 'direct'")

    # Add barnes_hut_theta field (default 0.5, standard opening angle)
    migrator.sql('ALTER TABLE job ADD COLUMN barnes_hut_theta REAL DEFAULT 0.5')

    # Backfill existing jobs as 'direct' algorithm
    migrator.sql("UPDATE job SET force_algorithm = 'direct' WHERE force_algorithm IS NULL")
    migrator.sql("UPDATE job SET barnes_hut_theta = 0.5 WHERE barnes_hut_theta IS NULL")

    # Add CHECK constraints for data integrity
    migrator.sql("ALTER TABLE job ADD CONSTRAINT chk_force_algorithm CHECK (force_algorithm IN ('direct', 'barnes_hut'))")
    migrator.sql("ALTER TABLE job ADD CONSTRAINT chk_barnes_hut_theta CHECK (barnes_hut_theta >= 0.1 AND barnes_hut_theta <= 2.0)")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove algorithm selection fields."""

    # Drop constraints first
    migrator.sql('ALTER TABLE job DROP CONSTRAINT IF EXISTS chk_force_algorithm')
    migrator.sql('ALTER TABLE job DROP CONSTRAINT IF EXISTS chk_barnes_hut_theta')

    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS force_algorithm')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS barnes_hut_theta')
