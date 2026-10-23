# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add handed_back_at to the job table.

A monolithic worker that hands a job back (graceful stop, retryable failure,
infrastructure outage) expires its lease and republishes the job. While that
message waits in a busy queue the watchdog sees the expired lease and
republishes the job too, counting a retry: a long outage or a rolling restart
could then fail a job that never failed. handed_back_at is set with the
expired lease and cleared when a worker takes the lease. While it is set the
watchdog still republishes (the hand-back publish may have failed) but does not
count a retry, since the worker already applied its own retry budget.
Existing rows read NULL.

Date: 2026-10-11
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add the nullable handed_back_at column."""
    # IF NOT EXISTS: run_migrations() raises on error, which would block API startup.
    migrator.sql("ALTER TABLE job ADD COLUMN IF NOT EXISTS handed_back_at TIMESTAMP WITH TIME ZONE NULL")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove the column."""
    migrator.sql("ALTER TABLE job DROP COLUMN IF EXISTS handed_back_at")
