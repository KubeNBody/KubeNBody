# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Backfill existing jobs into input_files table.

For each existing job with input_file_key, create corresponding InputFile record
and link the job to it. Handles deduplication of files used by multiple jobs.

Date: 2025-01-22
Related: File library feature (migration 007)
"""

from datetime import datetime

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    """Backfill input_files from existing job records."""

    if fake:
        return

    # Import models - need to define inline to avoid circular imports
    class InputFile(pw.Model):
        file_id = pw.AutoField(primary_key=True)
        user_friendly_name = pw.CharField(max_length=255, unique=True, index=True)
        original_filename = pw.CharField(max_length=255)
        s3_key = pw.CharField(unique=True, index=True)
        s3_bucket = pw.CharField(default='inputfiles')
        file_size = pw.IntegerField()
        num_bodies = pw.IntegerField()
        created_at = pw.DateTimeField()
        last_used_at = pw.DateTimeField()
        usage_count = pw.IntegerField(default=0)
        description = pw.CharField(max_length=1000, null=True)

        class Meta:
            table_name = 'input_files'

    class Job(pw.Model):
        job_id = pw.AutoField(primary_key=True)
        input_file_key = pw.CharField(null=True)
        input_file_id = pw.IntegerField(null=True)
        filename = pw.CharField(null=True)
        file_size = pw.IntegerField(null=True)
        created_at = pw.DateTimeField()
        updated_at = pw.DateTimeField()

        class Meta:
            table_name = 'job'

    # Bind database to models after class definition
    InputFile._meta.database = database
    Job._meta.database = database

    print("Starting input_files backfill migration...")

    # Query all jobs with input_file_key
    jobs = Job.select().where(Job.input_file_key.is_null(False))

    total_jobs = jobs.count()
    print(f"Found {total_jobs} jobs with input files to process")

    processed = 0
    created_files = 0
    linked_files = 0

    for job in jobs:
        # Skip if already linked
        if job.input_file_id is not None:
            print(f"Job {job.job_id} already linked to file {job.input_file_id}, skipping")
            continue

        # Check if InputFile already exists for this S3 key
        existing = InputFile.select().where(InputFile.s3_key == job.input_file_key).first()

        if existing:
            # Link job to existing file and update usage stats
            job.input_file_id = existing.file_id
            job.save()

            existing.usage_count += 1
            # Update last_used_at if this job was used more recently
            if job.updated_at and job.updated_at > existing.last_used_at:
                existing.last_used_at = job.updated_at
            existing.save()

            linked_files += 1
            print(f"Linked job {job.job_id} to existing file {existing.file_id} ({existing.user_friendly_name})")
        else:
            # Create new InputFile record
            # Generate user-friendly name from filename or job ID
            base_name = job.filename if job.filename else f"legacy_job_{job.job_id}"

            # Remove file extension
            if '.' in base_name:
                base_name = base_name.rsplit('.', 1)[0]

            # Sanitize name (remove special chars, replace spaces)
            import re
            sanitized_name = re.sub(r'[^\w\s\-_.]', '', base_name)
            sanitized_name = sanitized_name.replace(' ', '_')
            sanitized_name = re.sub(r'_+', '_', sanitized_name)
            sanitized_name = sanitized_name.strip('_')[:255]

            # Ensure uniqueness by appending suffix if needed
            original_name = sanitized_name
            counter = 1
            while InputFile.select().where(InputFile.user_friendly_name == sanitized_name).exists():
                sanitized_name = f"{original_name}_{counter}"
                counter += 1

            # Create the InputFile record
            try:
                file_record = InputFile.create(
                    user_friendly_name=sanitized_name,
                    original_filename=job.filename or 'unknown.csv',
                    s3_key=job.input_file_key,
                    s3_bucket='inputfiles',  # Default bucket
                    file_size=job.file_size or 0,
                    num_bodies=0,  # Cannot determine without downloading
                    created_at=job.created_at or datetime.utcnow(),
                    last_used_at=job.updated_at or job.created_at or datetime.utcnow(),
                    usage_count=1,
                    description=f"Migrated from job {job.job_id}"
                )

                # Link job to new file
                job.input_file_id = file_record.file_id
                job.save()

                created_files += 1
                print(f"Created file {file_record.file_id} ({sanitized_name}) for job {job.job_id}")
            except Exception as e:
                print(f"Error creating file record for job {job.job_id}: {e}")
                continue

        processed += 1

        # Progress update every 10 jobs
        if processed % 10 == 0:
            print(f"Progress: {processed}/{total_jobs} jobs processed")

    print("\nBackfill migration complete!")
    print(f"  Total jobs processed: {processed}")
    print(f"  New file records created: {created_files}")
    print(f"  Jobs linked to existing files: {linked_files}")


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    """Rollback backfill - remove input_file_id links from jobs.

    Note: This does NOT delete the InputFile records created during migration.
    If you need a full rollback, also rollback migration 007.
    """

    if fake:
        return

    print("Rolling back input_files backfill...")

    # Clear input_file_id from all jobs
    migrator.sql('UPDATE job SET input_file_id = NULL WHERE input_file_id IS NOT NULL')

    print("Backfill rollback complete - all job.input_file_id references cleared")
