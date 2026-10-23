# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Authentication utilities for JWT token management and password handling.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from peewee import IntegrityError

from config import settings
from logging_config import setup_logging
from models import User


# JWT configuration (from centralized config)
SECRET_KEY = settings.auth.jwt_secret_key
ALGORITHM = settings.auth.jwt_algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.auth.access_token_expire_minutes

# development default. check_production_secrets refuses it in the cluster
_DEFAULT_SECRET = "dev-secret-key-change-in-production-please"
_MIN_SECRET_LEN = 32   # init-secrets.sh generates 64 characters


def in_cluster() -> bool:
    """True inside a Kubernetes pod, where the development defaults must not be
    used: the default signing key and password are public in this repository."""
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST")) and os.environ.get("NBODY_ALLOW_DEV_SECRETS") != "1"


def check_production_secrets() -> None:
    """Refuse to start the API in a cluster with the default JWT signing key
    (anyone could forge tokens) or an empty or short one (an empty secret value
    passes the env var through as ""). The dashboard password is the operator's
    choice (the deployment keeps the existing one), so it is not checked
    here."""
    if not in_cluster():
        return
    missing = []
    if not SECRET_KEY or SECRET_KEY == _DEFAULT_SECRET or len(SECRET_KEY) < _MIN_SECRET_LEN:
        missing.append("JWT_SECRET_KEY")
    if missing:
        raise RuntimeError(
            f"Refusing to start: {', '.join(missing)} not set (the development defaults are public). "
            f"Provide them through the api-secrets ExternalSecret (Key Vault / Secret Manager)."
        )


if SECRET_KEY == _DEFAULT_SECRET:
    import logging
    logging.getLogger(__name__).warning(
        "SECURITY WARNING: Using default JWT_SECRET_KEY. "
        "Set JWT_SECRET_KEY environment variable in production!"
    )

# HTTP Bearer scheme for extracting tokens
security = HTTPBearer()


def _bcrypt_input(password: str) -> bytes:
    """The bytes bcrypt hashes: at most 72 (its limit), cut at a character
    boundary, as passlib (used before) was given them, so existing hashes
    still verify."""
    return password.encode('utf-8')[:72].decode('utf-8', errors='ignore').encode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a bcrypt hash (False for a malformed hash)."""
    try:
        return bcrypt.checkpw(_bcrypt_input(plain_password), hashed_password.encode('ascii'))
    except ValueError:
        return False


def validate_password(password: str) -> tuple[bool, str]:
    """(is_valid, error_message): 8 to 128 characters."""
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"
    if len(password) > 128:
        return False, "Password must be at most 128 characters long"
    return True, ""


def get_password_hash(password: str) -> str:
    """Generate a bcrypt hash of the password (12 rounds, $2b$, as passlib made)."""
    return bcrypt.hashpw(_bcrypt_input(password), bcrypt.gensalt(rounds=12)).decode('ascii')


def create_access_token(data: dict) -> str:
    """A signed JWT of `data`, expiring after ACCESS_TOKEN_EXPIRE_MINUTES."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({**data, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """The payload of a valid JWT, or None."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def authenticate_user(username: str, password: str) -> Optional[User]:
    """The active user with these credentials, or None. A password is
    verified even for an unknown username (against a dummy hash), so the
    timing does not reveal which usernames exist."""
    dummy_hash = "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5ck6xFGMXU5JO"

    user = None
    user_hash = dummy_hash

    try:
        user = User.get(User.username == username)
        user_hash = user.hashed_password
    except User.DoesNotExist:
        pass

    password_valid = verify_password(password, user_hash)
    if user and password_valid and user.is_active:
        return user

    return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> User:
    """FastAPI dependency: the user of the request's bearer token (401 if the
    token is invalid or the user unknown)."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    token = credentials.credentials
    payload = decode_access_token(token)

    if payload is None:
        raise credentials_exception

    username: str = payload.get("sub")
    if username is None:
        raise credentials_exception

    try:
        user = User.get(User.username == username)
    except User.DoesNotExist:
        raise credentials_exception

    if not user.is_active:
        raise credentials_exception

    return user


def create_default_user():
    """Create the dashboard account (DEFAULT_USER_USERNAME, default "user")
    with DEFAULT_USER_PASSWORD at startup if it doesn't exist. Without a
    password no account is created: there is no built-in one."""
    logger = setup_logging('auth')

    default_username = settings.api.default_user_username or "user"
    configured = settings.api.default_user_password
    if not configured:
        logger.warning("DEFAULT_USER_PASSWORD is not set: no default account created")
        return
    default_password = configured

    is_valid, error_msg = validate_password(default_password)
    if not is_valid:
        logger.error(f"Default user password validation failed: {error_msg}")
        logger.error("Using environment variable DEFAULT_USER_PASSWORD which does not meet requirements")
        return

    try:
        user = User.get(User.username == default_username)
        # The configured password is the source of truth: an account created
        # with another one is rotated on the next start (there is no
        # change-password endpoint)
        if not verify_password(configured, user.hashed_password):
            user.hashed_password = get_password_hash(configured)
            user.save()
            logger.warning(f"Default user '{default_username}': password updated from DEFAULT_USER_PASSWORD")
        else:
            logger.debug(f"Default user '{default_username}' already exists")
    except User.DoesNotExist:
        hashed_password = get_password_hash(default_password)
        try:
            User.create(
                username=default_username,
                hashed_password=hashed_password,
                is_active=True
            )
        except IntegrityError:
            # another API replica created it in the meantime
            logger.debug(f"Default user '{default_username}' created by another replica")
            return
        logger.info(f"Created default user: {default_username}")
