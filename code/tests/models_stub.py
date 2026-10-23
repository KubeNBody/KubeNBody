# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The `models` stand-in conftest.py installs before anything imports src/:
no test talks to PostgreSQL, and the tests that need tables build their own
over SQLite."""
import types
from datetime import datetime, timezone


def models_stub_module():
    """Any model name resolves (to `object`), and utcnow is the real clock.
    Tests that need a Job table monkeypatch their own over it."""
    stub = types.ModuleType("models")
    stub.__getattr__ = lambda name: object
    stub.utcnow = lambda: datetime.now(timezone.utc)
    return stub
