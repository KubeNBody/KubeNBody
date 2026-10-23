# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add next_time_step to the job table.

The step a v1 distributed job's next epoch runs at, written by the
orchestrator with the epoch's package rows. An adaptive job's step changes
every epoch and was not stored anywhere, so a resumed job restarted at its
base time_step. Existing rows read NULL (the base step).

Date: 2026-10-22
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add the nullable next_time_step column."""
    # IF NOT EXISTS: run_migrations() raises on error, which would block API startup.
    migrator.sql("ALTER TABLE job ADD COLUMN IF NOT EXISTS next_time_step DOUBLE PRECISION NULL")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove the column."""
    migrator.sql("ALTER TABLE job DROP COLUMN IF EXISTS next_time_step")
