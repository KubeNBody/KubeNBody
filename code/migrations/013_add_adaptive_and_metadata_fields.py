# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add adaptive timestepping, metadata, and block timestep fields to job table.

Date: 2026-04-13
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add adaptive timestepping, metadata, and block timestep fields."""

    # Adaptive timestepping fields
    migrator.sql('ALTER TABLE job ADD COLUMN adaptive_enabled BOOLEAN DEFAULT FALSE')
    migrator.sql('ALTER TABLE job ADD COLUMN adaptive_eta FLOAT DEFAULT 0.02')
    migrator.sql('ALTER TABLE job ADD COLUMN adaptive_dt_min FLOAT NULL')
    migrator.sql('ALTER TABLE job ADD COLUMN adaptive_dt_max FLOAT NULL')
    migrator.sql('ALTER TABLE job ADD COLUMN current_simulation_time FLOAT DEFAULT 0.0')

    # Reproducibility metadata
    migrator.sql('ALTER TABLE job ADD COLUMN metadata_json TEXT NULL')

    # Block timestep fields
    migrator.sql('ALTER TABLE job ADD COLUMN block_timestep_enabled BOOLEAN DEFAULT FALSE')
    migrator.sql('ALTER TABLE job ADD COLUMN block_timestep_max_rung INTEGER DEFAULT 8')

    # Backfill
    migrator.sql('UPDATE job SET adaptive_enabled = FALSE WHERE adaptive_enabled IS NULL')
    migrator.sql('UPDATE job SET current_simulation_time = 0.0 WHERE current_simulation_time IS NULL')
    migrator.sql('UPDATE job SET block_timestep_enabled = FALSE WHERE block_timestep_enabled IS NULL')


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Remove fields."""
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS adaptive_enabled')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS adaptive_eta')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS adaptive_dt_min')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS adaptive_dt_max')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS current_simulation_time')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS metadata_json')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS block_timestep_enabled')
    migrator.sql('ALTER TABLE job DROP COLUMN IF EXISTS block_timestep_max_rung')
