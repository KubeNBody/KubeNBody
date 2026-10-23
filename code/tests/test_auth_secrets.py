# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The API refuses the public development secrets inside a cluster."""
import sys
import types

import pytest

from helpers import use_models


def _auth(monkeypatch, key, password):
    # auth imports models, which connects to PostgreSQL. A stub is enough here
    if "auth" not in sys.modules:
        use_models(monkeypatch, types.SimpleNamespace(User=object))
    import auth
    monkeypatch.setattr(auth, "SECRET_KEY", key)
    monkeypatch.setattr(auth.settings.api, "default_user_password", password)
    return auth


def test_defaults_refused_in_cluster(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.delenv("NBODY_ALLOW_DEV_SECRETS", raising=False)
    auth = _auth(monkeypatch, "dev-secret-key-change-in-production-please", None)
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        auth.check_production_secrets()
    # the dashboard password is not checked
    for password in ("dev-password", None, "a-real-password"):
        auth = _auth(monkeypatch, "k" * 48, password)
        auth.check_production_secrets()


@pytest.mark.parametrize("key", ["", "short-key"])
def test_empty_or_short_key_refused_in_cluster(monkeypatch, key):
    """Found by the local stack check: an empty secret value arrives as
    JWT_SECRET_KEY="" and was accepted (tokens signed with an empty key)."""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.delenv("NBODY_ALLOW_DEV_SECRETS", raising=False)
    auth = _auth(monkeypatch, key, "a-real-password")
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        auth.check_production_secrets()


def test_defaults_allowed_outside_cluster(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    auth = _auth(monkeypatch, "dev-secret-key-change-in-production-please", None)
    auth.check_production_secrets()


def test_passwords_hashed_by_passlib_still_verify(monkeypatch):
    """Hashes stored by passlib verify with the bcrypt-based auth, and new
    hashes have the same form."""
    auth = _auth(monkeypatch, None, None)
    stored = "$2b$12$lrfRd8PMf9cV5wIRT0p1z.0b/rjUBKfaKtZW4jWIhb5EA4o0iAhFq"   # passlib, 'correct horse battery'
    assert auth.verify_password("correct horse battery", stored)
    assert not auth.verify_password("wrong horse battery", stored)
    assert not auth.verify_password("anything", "not a bcrypt hash")
    new = auth.get_password_hash("ż" * 40)                  # 80 bytes: cut at a character boundary
    assert new.startswith("$2b$12$") and auth.verify_password("ż" * 40, new)
