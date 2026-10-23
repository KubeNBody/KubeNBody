# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The FastAPI routers, by domain."""

from .auth import router as auth_router
from .files import router as files_router
from .jobs import router as jobs_router
from .monitoring import router as monitoring_router


__all__ = ['auth_router', 'files_router', 'jobs_router', 'monitoring_router']
