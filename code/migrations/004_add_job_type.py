# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Add job_type field to Job model to distinguish distributed from monolithic jobs.
"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """
    Add job_type field with default 'distributed'.
    """
    migrator.add_fields(
        'job',
        job_type=pw.CharField(default='distributed')
    )


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """
    Remove job_type field.
    """
    migrator.remove_fields('job', 'job_type')
