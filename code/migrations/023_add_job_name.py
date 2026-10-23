# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add per-job name column.

A short title for a job, shown in place of its input file name in job lists
and headers, so that runs sharing an input file ("figure_eight.csv") can be
told apart. Optional: jobs without one keep showing the file name.
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add nullable name column to job table."""
    migrator.sql("ALTER TABLE job ADD COLUMN name VARCHAR(120) NULL")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove name column."""
    migrator.sql("ALTER TABLE job DROP COLUMN IF EXISTS name")
