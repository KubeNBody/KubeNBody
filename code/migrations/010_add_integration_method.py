# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add integration method selection field to job table.

Adds support for selecting the numerical integration method per job:
- integration_method: 'simple_euler' or 'symplectic_euler' (default)

Date: 2025-01-13
Related: Configurable integration methods implementation
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add integration method selection field."""

    # Add integration_method field ('symplectic_euler' for the existing rows)
    migrator.sql("ALTER TABLE job ADD COLUMN integration_method VARCHAR(30) DEFAULT 'symplectic_euler'")

    # Backfill existing jobs as 'symplectic_euler' (current behavior)
    migrator.sql("UPDATE job SET integration_method = 'symplectic_euler' WHERE integration_method IS NULL")

    # Add CHECK constraint for data integrity
    migrator.sql("ALTER TABLE job ADD CONSTRAINT chk_integration_method CHECK (integration_method IN ('simple_euler', 'symplectic_euler'))")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove integration method field."""

    # Drop constraint first
    migrator.sql('ALTER TABLE job DROP CONSTRAINT IF EXISTS chk_integration_method')

    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS integration_method')
