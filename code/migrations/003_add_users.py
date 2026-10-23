# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Peewee migrations -- 003_add_users.py.

Create users table for authentication.

"""

from contextlib import suppress

import peewee as pw
from peewee_migrate import Migrator


with suppress(ImportError):
    pass


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Write your migrations here."""

    @migrator.create_model
    class User(pw.Model):
        user_id = pw.AutoField()
        username = pw.CharField(max_length=100, unique=True, index=True)
        hashed_password = pw.CharField(max_length=255)
        is_active = pw.BooleanField(default=True)
        created_at = pw.DateTimeField()

        class Meta:
            table_name = "user"


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Write your rollback migrations here."""

    migrator.remove_model('user')
