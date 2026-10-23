# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add recovery_count to the job table.

retry_count is a retry budget: the monolithic worker resets it on every
progress checkpoint and at finalize, so it cannot tell afterwards whether a run
was interrupted. recovery_count only ever increases: a monolithic worker bumps
it when it resumes a job that an earlier lease had already started
(redelivery, watchdog republish, lease takeover), and the distributed recover
endpoint bumps it on every recovery. A run with recovery_count > 0 was
interrupted, and its duration includes the stall and the recomputation.
Existing rows read 0.

Date: 2026-09-28
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add the recovery_count column (NOT NULL DEFAULT 0)."""
    # IF NOT EXISTS: run_migrations() raises on error, which would block API startup.
    migrator.sql("ALTER TABLE job ADD COLUMN IF NOT EXISTS recovery_count INTEGER NOT NULL DEFAULT 0")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove the column."""
    migrator.sql("ALTER TABLE job DROP COLUMN IF EXISTS recovery_count")
