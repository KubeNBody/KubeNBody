# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add rk4 and forest_ruth to valid integration methods.

Updates the CHECK constraint on integration_method to include:
- 'rk4' (4th-order Runge-Kutta, non-symplectic)
- 'forest_ruth' (4th-order symplectic, CPU-only)

These integrators were added to the application code without a corresponding
DB constraint update, causing submissions with these methods to fail at INSERT.
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add rk4 and forest_ruth to valid integration methods."""

    migrator.sql('ALTER TABLE job DROP CONSTRAINT IF EXISTS chk_integration_method')

    migrator.sql(
        "ALTER TABLE job ADD CONSTRAINT chk_integration_method "
        "CHECK (integration_method IN ('simple_euler', 'symplectic_euler', 'velocity_verlet', 'rk4', 'forest_ruth'))"
    )


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Revert to constraint without rk4 and forest_ruth."""

    migrator.sql('ALTER TABLE job DROP CONSTRAINT IF EXISTS chk_integration_method')

    migrator.sql(
        "ALTER TABLE job ADD CONSTRAINT chk_integration_method "
        "CHECK (integration_method IN ('simple_euler', 'symplectic_euler', 'velocity_verlet'))"
    )
