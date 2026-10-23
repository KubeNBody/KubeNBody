# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add package partitioning fields to the job table.

The V2 distributed flow can permute the body order once at job creation so
contiguous package slices take a chosen shape:
- 'linear'  : file order (default, identical to the historical behaviour)
- 'random'  : seeded shuffle (load balance, reproducible via partition_seed)
- 'spatial' : Morton/Z-order locality (keeps interacting neighbours together)

These are recorded on the job for reproducibility and reporting. Existing rows
default to 'linear' / seed 0, i.e. byte-identical to how they actually ran.

Date: 2026-08-17
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add partition_strategy + partition_seed columns. The defaults keep the
    existing rows' contiguous split."""
    # IF NOT EXISTS so a re-run (or a column added by hand)
    # can't crash API startup — run_migrations() raises, which would block boot.
    migrator.sql(
        "ALTER TABLE job ADD COLUMN IF NOT EXISTS partition_strategy VARCHAR(255) DEFAULT 'linear'"
    )
    migrator.sql("ALTER TABLE job ADD COLUMN IF NOT EXISTS partition_seed INTEGER DEFAULT 0")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove the partitioning columns."""
    migrator.sql("ALTER TABLE job DROP COLUMN IF EXISTS partition_strategy")
    migrator.sql("ALTER TABLE job DROP COLUMN IF EXISTS partition_seed")
