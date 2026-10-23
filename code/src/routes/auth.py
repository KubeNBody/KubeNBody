# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Endpoints for user authentication and token management.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from auth import authenticate_user, create_access_token, get_current_user
from auth_schemas import LoginRequest, TokenResponse, UserResponse
from db_threads import ClosingRoute
from logging_config import setup_logging
from models import User


logger = setup_logging('api.auth')

router = APIRouter(prefix="/api/auth", tags=["Authentication"], route_class=ClosingRoute)


@router.post("/login", response_model=TokenResponse)
def login(credentials: LoginRequest):  # sync: bcrypt runs in the threadpool
    """Authenticate a user and return a JWT access token (401 if the
    credentials are invalid)."""
    user = authenticate_user(credentials.username, credentials.password)

    if not user:
        logger.warning(f"Failed login attempt for username: {credentials.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    logger.info(f"Successful login for user: {user.username}")

    access_token = create_access_token(
        data={"sub": user.username}
    )

    return TokenResponse(access_token=access_token, token_type="bearer")


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    """The authenticated user."""
    return UserResponse(
        user_id=current_user.user_id,
        username=current_user.username,
        is_active=current_user.is_active,
        created_at=current_user.created_at
    )
