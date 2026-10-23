# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Pydantic schemas for authentication API endpoints.
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    """Request body for login endpoint."""
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "username": "user",
            "password": "your-password",
        }
    })

    username: str = Field(..., min_length=1, max_length=100, description="Username")
    password: str = Field(..., min_length=8, max_length=128, description="Password (minimum 8 characters)")


class TokenResponse(BaseModel):
    """Response body for successful login."""
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "access_token": "<JWT>",
            "token_type": "bearer",
        }
    })

    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field(default="bearer", description="Token type (always 'bearer')")


class UserResponse(BaseModel):
    """Response body for user information."""
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "user_id": 1,
            "username": "user",
            "is_active": True,
            "created_at": "2025-01-01T00:00:00",
        }
    })

    user_id: int = Field(..., description="Unique user ID")
    username: str = Field(..., description="Username")
    is_active: bool = Field(..., description="Whether user account is active")
    created_at: datetime = Field(..., description="When user was created")
