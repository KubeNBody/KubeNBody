# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Workers wait for the API's migration instead of failing on new model columns."""
import ast
import types

from helpers import REPO, use_models
from job_timestamps import wait_for_job_schema


def _fake_models(monkeypatch, present_sequence):
    cols = {"job_id", "status", "started_at", "completed_at"}
    fields = [types.SimpleNamespace(column_name=c) for c in sorted(cols)]
    calls = {"n": 0}

    class DB:
        @staticmethod
        def get_columns(table):
            present = present_sequence[min(calls["n"], len(present_sequence) - 1)]
            calls["n"] += 1
            return [types.SimpleNamespace(name=c) for c in present]

    job = types.SimpleNamespace(_meta=types.SimpleNamespace(sorted_fields=fields, table_name="job"))
    use_models(monkeypatch, types.SimpleNamespace(Job=job, db=DB()))
    return calls


def test_returns_once_the_migration_has_run(monkeypatch):
    old = {"job_id", "status"}
    new = {"job_id", "status", "started_at", "completed_at"}
    calls = _fake_models(monkeypatch, [old, old, new])
    assert wait_for_job_schema(poll_s=0.0) is True
    assert calls["n"] == 3


def test_times_out_while_columns_are_missing(monkeypatch):
    _fake_models(monkeypatch, [{"job_id", "status"}])
    assert wait_for_job_schema(poll_s=0.0, timeout_s=0.0) is False


def _migration_sql(path):
    """function name -> the SQL statements it passes to migrator.sql (from the
    code, not the docstrings)."""
    tree = ast.parse(path.read_text())
    return {f.name: " ".join(c.args[0].value for c in ast.walk(f)
                             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                             and c.func.attr == "sql")
            for f in tree.body if isinstance(f, ast.FunctionDef)}


def test_migration_021_and_model_field():
    sql = _migration_sql(REPO / "code/migrations/021_add_job_recovery_count.py")
    assert "ADD COLUMN IF NOT EXISTS recovery_count INTEGER NOT NULL DEFAULT 0" in sql["migrate"]
    assert "DROP COLUMN IF EXISTS recovery_count" in sql["rollback"]
    tree = ast.parse((REPO / "code/src/models.py").read_text())
    job = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Job")
    fields = {t.id for n in job.body if isinstance(n, ast.Assign) for t in n.targets
              if isinstance(t, ast.Name)}
    assert "recovery_count" in fields     # so wait_for_job_schema waits for 021
