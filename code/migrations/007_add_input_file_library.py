# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add input file library for reusable file management.

Creates input_files table for storing uploaded files with user-friendly names
and adds foreign key reference from jobs to input_files table.

This enables:
- Reusable file uploads across multiple jobs
- User-friendly file names instead of UUID-based keys
- File usage tracking and metadata

Date: 2025-01-22
Related: File library feature
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Create input_files table and add foreign key to job table."""

    # Create input_files table
    migrator.sql('''
        CREATE TABLE IF NOT EXISTS input_files (
            file_id SERIAL PRIMARY KEY,
            user_friendly_name VARCHAR(255) NOT NULL UNIQUE,
            original_filename VARCHAR(255) NOT NULL,
            s3_key VARCHAR(1024) NOT NULL UNIQUE,
            s3_bucket VARCHAR(255) NOT NULL DEFAULT 'inputfiles',
            file_size INTEGER NOT NULL,
            num_bodies INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            usage_count INTEGER NOT NULL DEFAULT 0,
            description VARCHAR(1000) NULL
        )
    ''')

    # Create indexes for efficient querying
    migrator.sql('CREATE UNIQUE INDEX IF NOT EXISTS idx_input_files_name ON input_files(user_friendly_name)')
    migrator.sql('CREATE UNIQUE INDEX IF NOT EXISTS idx_input_files_s3key ON input_files(s3_key)')
    migrator.sql('CREATE INDEX IF NOT EXISTS idx_input_files_created ON input_files(created_at DESC)')
    migrator.sql('CREATE INDEX IF NOT EXISTS idx_input_files_last_used ON input_files(last_used_at DESC)')

    # Add foreign key reference to job table (nullable for backwards compatibility)
    migrator.sql('ALTER TABLE job ADD COLUMN input_file_id INTEGER NULL')

    # Create index for foreign key lookups
    migrator.sql('CREATE INDEX IF NOT EXISTS idx_job_input_file ON job(input_file_id)')

    # Note: Foreign key constraint NOT added to allow flexibility during migration
    # and to avoid cascading delete issues. Application logic will maintain referential integrity.


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove input file library tables and references."""

    # Remove foreign key column from job table
    migrator.sql('DROP INDEX IF EXISTS idx_job_input_file')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS input_file_id')

    # Drop input_files table (will cascade delete all records)
    migrator.sql('DROP TABLE IF EXISTS input_files CASCADE')
