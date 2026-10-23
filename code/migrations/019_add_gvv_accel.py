# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Add gvv_accel (velocity Verlet with the acceleration background predictor).

With the acceleration predictor the distributed velocity Verlet step reproduces
the monolithic step exactly (the tau=dt Taylor prediction coincides with the
Verlet drift), and the shipped epoch-start accelerations double as the
package's cached acceleration, so a task needs one force evaluation instead of
two. Extends the CHECK constraint from migration 017.
"""

import peewee as pw
from peewee_migrate import Migrator


_BASE = (
    "'simple_euler', 'symplectic_euler', 'velocity_verlet', 'rk4', 'forest_ruth', "
    "'gvv_frozen', 'gvv_linear', "
    "'grk4_frozen', 'grk4_linear', 'grk4_accel', "
    "'gfr_frozen', 'gfr_linear', 'gfr_accel'"
)


def _set_constraint(migrator: Migrator, methods: str) -> None:
    migrator.sql('ALTER TABLE job DROP CONSTRAINT IF EXISTS chk_integration_method')
    migrator.sql(
        "ALTER TABLE job ADD CONSTRAINT chk_integration_method "
        f"CHECK (integration_method IN ({methods}))"
    )


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Allow gvv_accel."""
    _set_constraint(migrator, _BASE + ", 'gvv_accel'")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Revert to the migration-017 constraint."""
    _set_constraint(migrator, _BASE)
