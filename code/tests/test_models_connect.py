# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""src/models.py opens no connection at import (code without a PostgreSQL
imports it through file_service), and its first
connection retries until PostgreSQL answers. Run in fresh interpreters: the
conftest replaces `models` with a stub in this one."""
import os
import socket
import subprocess
import sys


SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(code: str, retries: int) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=SRC, POSTGRES_HOST="127.0.0.1", POSTGRES_PORT=str(_closed_port()),
               DB_MAX_RETRIES=str(retries), DB_RETRY_DELAY="0.2")
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)


def test_importing_models_and_file_service_opens_no_connection():
    r = _run("import models, file_service; assert models.db.is_closed(); print('ok')", retries=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1] == "ok"
    assert "Attempting to connect" not in r.stdout + r.stderr


_FIRST_QUERY = ("import models\n"
                "from transient_errors import is_transient_error\n"
                "try:\n"
                "    models.Job.select().count()\n"
                "except Exception as e:\n"
                "    print(type(e).__name__, is_transient_error(e), e)\n")


def test_first_connection_retries_then_gives_up_as_a_transient_error():
    r = _run(_FIRST_QUERY, retries=3)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1].startswith("OperationalError True Could not connect to PostgreSQL")
    assert (r.stdout + r.stderr).count("Attempting to connect") == 3


def test_zero_retries_still_tries_once():
    r = _run(_FIRST_QUERY, retries=0)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1].startswith("OperationalError True Could not connect to PostgreSQL")
    assert (r.stdout + r.stderr).count("Attempting to connect") == 1
