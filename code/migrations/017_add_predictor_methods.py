# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add the distributed predictor-family methods to valid integration methods.

Updates the CHECK constraint on integration_method to include the g-named
predictor ladder used by the distributed (package) flow:
- gvv_frozen, gvv_linear                          (velocity-verlet base)
- grk4_frozen, grk4_linear, grk4_accel            (rk4 base)
- gfr_frozen, gfr_linear, gfr_accel               (forest-ruth base)

These were added to the application enum (IntegrationMethod) and
VALID_INTEGRATION_METHODS without a corresponding DB constraint update, so
distributed jobs using any g-method failed at INSERT with a
chk_integration_method CheckViolation (only the 5 base methods from migration
016 were allowed). Mirrors the base-method list in migration 016.
"""

import peewee as pw
from peewee_migrate import Migrator


_ALL_METHODS = (
    "'simple_euler', 'symplectic_euler', 'velocity_verlet', 'rk4', 'forest_ruth', "
    "'gvv_frozen', 'gvv_linear', "
    "'grk4_frozen', 'grk4_linear', 'grk4_accel', "
    "'gfr_frozen', 'gfr_linear', 'gfr_accel'"
)


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Add the g-named predictor methods to valid integration methods."""

    migrator.sql('ALTER TABLE job DROP CONSTRAINT IF EXISTS chk_integration_method')

    migrator.sql(
        "ALTER TABLE job ADD CONSTRAINT chk_integration_method "
        f"CHECK (integration_method IN ({_ALL_METHODS}))"
    )


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Revert to the migration-016 constraint (5 base methods only)."""

    migrator.sql('ALTER TABLE job DROP CONSTRAINT IF EXISTS chk_integration_method')

    migrator.sql(
        "ALTER TABLE job ADD CONSTRAINT chk_integration_method "
        "CHECK (integration_method IN ('simple_euler', 'symplectic_euler', 'velocity_verlet', 'rk4', 'forest_ruth'))"
    )
