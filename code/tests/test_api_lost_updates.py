# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Metadata edits and usage counters must not revert concurrent writes
(read-modify-save of a whole row): the routes run over SQLite tables with
the columns involved (no test talks to PostgreSQL)."""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi import HTTPException
from peewee import AutoField, CharField, DateTimeField, IntegerField, Model, SqliteDatabase, TextField
from pydantic import ValidationError

import routes.files as F
import routes.jobs as J
import utils.input_files as IF
from api_schemas import JobDescriptionUpdate, JobNameUpdate
from constants import JOB_NAME_MAX
from helpers import SRC


@pytest.fixture
def sqlite_models(monkeypatch):
    db = SqliteDatabase(":memory:")

    class Job(Model):
        job_id = AutoField()
        status = CharField()
        current_epoch = IntegerField(default=0)
        results_file_key = CharField(null=True)
        description = TextField(null=True)
        name = CharField(null=True)
        updated_at = DateTimeField(null=True)

        class Meta:
            database = db

    class InputFile(Model):
        file_id = AutoField()
        user_friendly_name = CharField(unique=True)
        original_filename = CharField()
        num_bodies = IntegerField()
        file_size = IntegerField()
        created_at = DateTimeField(default=dt.datetime.now)
        last_used_at = DateTimeField(default=dt.datetime.now)
        usage_count = IntegerField(default=0)
        description = CharField(null=True)

        class Meta:
            database = db

    db.create_tables([Job, InputFile])
    monkeypatch.setattr(J, "Job", Job)
    monkeypatch.setattr(J, "_job_to_response", lambda job: job)
    monkeypatch.setattr(J, "utcnow", dt.datetime.now)   # naive: SQLite drops the zone
    for mod in (F, IF):
        monkeypatch.setattr(mod, "InputFile", InputFile)
    yield Job, InputFile
    db.close()


def _job(Job, **kw):
    fields = dict(status="IN_PROGRESS", current_epoch=2,
                  updated_at=dt.datetime(2026, 1, 1))
    fields.update(kw)
    return Job.create(**fields)


def _file(InputFile, **kw):
    fields = dict(user_friendly_name="f", original_filename="f.csv", file_size=10, num_bodies=3,
                  usage_count=1)
    fields.update(kw)
    return InputFile.create(**fields)


def test_description_edit_keeps_a_concurrent_worker_write(sqlite_models, monkeypatch):
    Job, _ = sqlite_models
    job = _job(Job)
    real_get = Job.get

    def get_then_worker_completes(*a, **k):
        row = real_get(*a, **k)
        Job.update(status="COMPLETED", results_file_key="r.csv", current_epoch=10).where(
            Job.job_id == job.job_id).execute()
        return row
    monkeypatch.setattr(Job, "get", get_then_worker_completes)
    J.update_job_description(job.job_id, JobDescriptionUpdate(description="  run A  "), current_user=None)
    monkeypatch.setattr(Job, "get", real_get)
    row = real_get(Job.job_id == job.job_id)
    assert (row.description, row.status, row.results_file_key, row.current_epoch) == (
        "run A", "COMPLETED", "r.csv", 10)
    # updated_at is the watchdog's progress signal and v1's completion time
    assert row.updated_at == dt.datetime(2026, 1, 1)


def test_description_edit_of_a_missing_job_is_404(sqlite_models):
    with pytest.raises(HTTPException) as e:
        J.update_job_description(99, JobDescriptionUpdate(description="x"), current_user=None)
    assert e.value.status_code == 404


def test_name_edit_is_one_line_and_keeps_other_columns(sqlite_models):
    Job, _ = sqlite_models
    job = _job(Job, description="note")
    J.update_job_name(job.job_id, JobNameUpdate(name="  Figure eight\n RK4  "), current_user=None)
    row = Job.get(Job.job_id == job.job_id)
    assert (row.name, row.description, row.status) == ("Figure eight RK4", "note", "IN_PROGRESS")
    assert row.updated_at == dt.datetime(2026, 1, 1)
    J.update_job_name(job.job_id, JobNameUpdate(name="   "), current_user=None)
    assert Job.get(Job.job_id == job.job_id).name is None


def test_name_edit_of_a_missing_job_is_404(sqlite_models):
    with pytest.raises(HTTPException) as e:
        J.update_job_name(99, JobNameUpdate(name="x"), current_user=None)
    assert e.value.status_code == 404


def test_a_job_name_is_capped_at_its_column_length():
    JobNameUpdate(name="x" * JOB_NAME_MAX)
    with pytest.raises(ValidationError):
        JobNameUpdate(name="x" * (JOB_NAME_MAX + 1))


def test_file_edit_keeps_a_concurrent_usage_count(sqlite_models, monkeypatch):
    _, InputFile = sqlite_models
    f = _file(InputFile)
    real = InputFile.get_or_none
    calls = []

    def read_then_submission_counts(*a, **k):
        row = real(*a, **k)
        if not calls:
            calls.append(1)
            InputFile.update(usage_count=InputFile.usage_count + 1).where(
                InputFile.file_id == f.file_id).execute()
        return row
    monkeypatch.setattr(InputFile, "get_or_none", read_then_submission_counts)
    out = F.update_file(f.file_id, user_friendly_name="renamed", description="d", clear_description=False,
                        current_user=None)
    assert (out.user_friendly_name, out.description, out.usage_count) == ("renamed", "d", 2)


def test_a_file_description_can_be_cleared(sqlite_models):
    """An empty description form field reads as absent to FastAPI, so the
    dashboard's cleared description was silently kept."""
    _, InputFile = sqlite_models
    f = _file(InputFile, description="old")
    out = F.update_file(f.file_id, user_friendly_name=None, description=None, clear_description=True,
                        current_user=None)
    assert out.description is None and InputFile.get_by_id(f.file_id).description is None


def test_file_use_is_counted_in_the_database(sqlite_models):
    """Both submit endpoints count uses through utils.input_files."""
    _, InputFile = sqlite_models
    f = _file(InputFile, usage_count=4)
    stale = InputFile.get_by_id(f.file_id)
    IF.record_file_use(f.file_id)
    IF.record_file_use(stale.file_id)      # a second submission that read the row earlier
    assert InputFile.get_by_id(f.file_id).usage_count == 6


@pytest.mark.parametrize("path", ["routes/jobs.py", "v2/api_v2.py", "routes/files.py", "utils/input_files.py"])
def test_no_read_modify_save_of_counters(path):
    src = (SRC / path).read_text()
    assert "usage_count += 1" not in src and "\n        file.save()" not in src


def test_a_multi_line_description_is_stored_as_typed(sqlite_models):
    """A browser's form encoding sends line breaks as CRLF. The description is
    stored with the line breaks the user typed."""
    _, InputFile = sqlite_models
    f = _file(InputFile)
    out = F.update_file(f.file_id, user_friendly_name=None, description=" a\r\nb ", clear_description=False,
                        current_user=None)
    assert out.description == "a\nb"
