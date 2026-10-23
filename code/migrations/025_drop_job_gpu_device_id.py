# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Drop gpu_device_id from the job table.

Added with the GPU fields (migration 006) for the CUDA device a job ran on,
it was never written: every row is NULL.

Date: 2026-10-22
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Drop the never-written gpu_device_id column."""
    # IF EXISTS: run_migrations() raises on error, which would block API startup.
    migrator.sql("ALTER TABLE job DROP COLUMN IF EXISTS gpu_device_id")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Restore the (empty) column."""
    migrator.sql("ALTER TABLE job ADD COLUMN IF NOT EXISTS gpu_device_id INTEGER NULL")
