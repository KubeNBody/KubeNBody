# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add velocity_verlet to valid integration methods.

Updates the CHECK constraint on integration_method to include 'velocity_verlet':
- integration_method: 'simple_euler', 'symplectic_euler', or 'velocity_verlet'

Velocity Verlet is a 2nd-order symplectic integrator that provides better
energy conservation than 1st-order Euler methods for long simulations.

Date: 2025-02-03
Related: Velocity Verlet integration implementation
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add velocity_verlet to valid integration methods."""

    # Drop the old CHECK constraint
    migrator.sql('ALTER TABLE job DROP CONSTRAINT IF EXISTS chk_integration_method')

    # Add new CHECK constraint with velocity_verlet included
    migrator.sql(
        "ALTER TABLE job ADD CONSTRAINT chk_integration_method "
        "CHECK (integration_method IN ('simple_euler', 'symplectic_euler', 'velocity_verlet'))"
    )


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Revert to original CHECK constraint (without velocity_verlet)."""

    # Drop the new constraint
    migrator.sql('ALTER TABLE job DROP CONSTRAINT IF EXISTS chk_integration_method')

    # Restore original constraint
    migrator.sql(
        "ALTER TABLE job ADD CONSTRAINT chk_integration_method "
        "CHECK (integration_method IN ('simple_euler', 'symplectic_euler'))"
    )
