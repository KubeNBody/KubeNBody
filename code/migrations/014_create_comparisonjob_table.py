# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Create ComparisonJob table for persistent comparison jobs.

Date: 2026-04-13
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Create comparisonjob table."""

    migrator.sql('''
        CREATE TABLE IF NOT EXISTS comparisonjob (
            comparison_id SERIAL PRIMARY KEY,
            reference_job_id INTEGER NOT NULL,
            comparison_job_ids VARCHAR(255) NOT NULL,
            max_snapshots INTEGER DEFAULT 100,
            status VARCHAR(50) DEFAULT 'PENDING',
            error_message VARCHAR(500) NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            started_at TIMESTAMP NULL,
            computation_time_ms INTEGER NULL,
            results_file_key VARCHAR(255) NULL,
            jobs_compared INTEGER DEFAULT 0,
            jobs_skipped INTEGER DEFAULT 0,
            job_fingerprints TEXT NULL
        )
    ''')
    migrator.sql('CREATE INDEX IF NOT EXISTS idx_comparisonjob_status ON comparisonjob(status)')
    migrator.sql('CREATE INDEX IF NOT EXISTS idx_comparisonjob_reference ON comparisonjob(reference_job_id)')
    migrator.sql('CREATE INDEX IF NOT EXISTS idx_comparisonjob_created ON comparisonjob(created_at)')


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Drop comparisonjob table."""
    migrator.sql('DROP TABLE IF EXISTS comparisonjob')
