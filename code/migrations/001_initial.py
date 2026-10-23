# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Peewee migrations -- 001_initial.py.

Some examples (model - class or model name)::

    > Model = migrator.orm['table_name']            # Return model in current state by name
    > Model = migrator.ModelClass                   # Return model in current state by name

    > migrator.sql(sql)                             # Run custom SQL
    > migrator.run(func, *args, **kwargs)           # Run python function with the given args
    > migrator.create_model(Model)                  # Create a model (could be used as decorator)
    > migrator.remove_model(model, cascade=True)    # Remove a model
    > migrator.add_fields(model, **fields)          # Add fields to a model
    > migrator.change_fields(model, **fields)       # Change fields
    > migrator.remove_fields(model, *field_names, cascade=True)
    > migrator.rename_field(model, old_field_name, new_field_name)
    > migrator.rename_table(model, new_table_name)
    > migrator.add_index(model, *col_names, unique=False)
    > migrator.add_not_null(model, *field_names)
    > migrator.add_default(model, field_name, default)
    > migrator.add_constraint(model, name, sql)
    > migrator.drop_index(model, *col_names)
    > migrator.drop_not_null(model, *field_names)
    > migrator.drop_constraints(model, *constraints)

"""

from contextlib import suppress

import peewee as pw
from peewee_migrate import Migrator


with suppress(ImportError):
    pass


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Write your migrations here."""
    
    @migrator.create_model
    class Job(pw.Model):
        job_id = pw.AutoField()
        num_of_epochs = pw.IntegerField()
        current_epoch = pw.IntegerField(default=0)
        package_size = pw.IntegerField()
        num_of_packages = pw.IntegerField()
        input_file_key = pw.CharField(max_length=255)
        results_file_key = pw.CharField(max_length=255, null=True)
        status = pw.CharField(default='PENDING', max_length=255)
        time_step = pw.FloatField(default=0.0001)
        snapshot_interval = pw.IntegerField(default=5)

        class Meta:
            table_name = "job"
            indexes = [(('status',), False)]

    @migrator.create_model
    class Package(pw.Model):
        package_id = pw.AutoField()
        package_number = pw.IntegerField()
        epoch_num = pw.IntegerField()
        job = pw.ForeignKeyField(column_name='job_id', field='job_id', model=migrator.orm['job'])
        file_key = pw.CharField(max_length=255, null=True)
        status = pw.CharField(default='PENDING', max_length=255)

        class Meta:
            table_name = "package"
            indexes = [(('job', 'epoch_num', 'status'), False), (('status',), False), (('job', 'status'), False)]


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Write your rollback migrations here."""
    
    migrator.remove_model('package')

    migrator.remove_model('job')
