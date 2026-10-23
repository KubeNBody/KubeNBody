# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add per-job description column.

Free-text user annotation for a Job, distinct from InputFile.description
(which is per-file). Lets users label individual runs that share an input
file ("baseline", "high-eps test", etc.).
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add nullable TEXT description column to job table."""
    migrator.sql("ALTER TABLE job ADD COLUMN description TEXT NULL")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove description column."""
    migrator.sql("ALTER TABLE job DROP COLUMN IF EXISTS description")
