# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add started_at / completed_at to the job table.

Durations used to be taken as updated_at - created_at, i.e. from submission to
the last update, which includes queueing before a worker picks the job up and
any later update of the row. started_at is set once, when a worker first starts
computing (monolithic: lease acquired, distributed: first epoch-1 task), and
completed_at when the job is finalized. Existing rows stay NULL.

Date: 2026-09-26
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add nullable started_at / completed_at columns."""
    # IF NOT EXISTS: run_migrations() raises on error, which would block API startup.
    migrator.sql("ALTER TABLE job ADD COLUMN IF NOT EXISTS started_at TIMESTAMP NULL")
    migrator.sql("ALTER TABLE job ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP NULL")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove the columns."""
    migrator.sql("ALTER TABLE job DROP COLUMN IF EXISTS started_at")
    migrator.sql("ALTER TABLE job DROP COLUMN IF EXISTS completed_at")
