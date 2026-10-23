# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Database migration management using peewee-migrate."""

import os

from peewee import PostgresqlDatabase
from peewee_migrate import Router

import models  # noqa: F401 -- imported to register all models
from models import db


MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'migrations')
# Arbitrary key of the PostgreSQL advisory lock that serialises migration runs
MIGRATION_LOCK_KEY = 0x4E424459  # "NBDY"

# Note: two migrations share the number 017 (017_add_job_description,
# 017_add_predictor_methods). They are applied in name order and recorded by
# name, so renaming either would re-run it on existing databases: keep both.


def get_router():
    """Get configured migration router."""
    return Router(db, migrate_dir=MIGRATIONS_DIR)


def run_migrations():
    """Run all pending migrations, one process at a time: API replicas
    starting together would otherwise apply the same migration twice. The
    lock is transaction-scoped, so it also holds behind a pgBouncer in
    transaction pooling mode, and the whole run is one transaction (no migration
    uses DDL that cannot run in one)."""
    router = get_router()
    with db.atomic():
        if isinstance(db, PostgresqlDatabase):
            db.execute_sql('SELECT pg_advisory_xact_lock(%s)', (MIGRATION_LOCK_KEY,))
        router.run()


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == 'create':
        # Create new migration: python src/migrate.py create migration_name
        name = sys.argv[2] if len(sys.argv) > 2 else 'auto'
        get_router().create(name, auto=['models'])
    else:
        # Run migrations: python src/migrate.py
        run_migrations()
