# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add configurable softening parameter to job table.

Date: 2026-05-18
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add softening_sq column with default 1e-9 (legacy value for existing jobs)."""
    migrator.sql("ALTER TABLE job ADD COLUMN softening_sq FLOAT DEFAULT 1e-9")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove softening_sq column."""
    migrator.sql("ALTER TABLE job DROP COLUMN IF EXISTS softening_sq")
