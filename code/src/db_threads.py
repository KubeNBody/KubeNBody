# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Database connections of worker threads.

The pool (models.db) hands each thread its own connection and reclaims none:
a thread that ends while holding one (FastAPI's threadpool retires idle
threads, a job thread finishes) leaks it, and once max_connections are leaked
every query waits and fails. Code that runs in such a thread returns its
connection with closes_connection.
"""
import functools
import inspect

from fastapi.routing import APIRoute
from peewee import Database

from models import db


def close_thread_connection() -> None:
    """Return this thread's pooled connection, if it holds one."""
    if isinstance(db, Database) and not db.is_closed():
        db.close()


def closes_connection(fn):
    """Wrap a function run in a worker thread (a sync handler, an
    asyncio.to_thread target, a job thread) to return its connection."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            close_thread_connection()
    return wrapper


class ClosingRoute(APIRoute):
    """An APIRoute whose sync handlers return their connection.

    FastAPI runs a plain `def` handler in its threadpool. The wrapper closes
    the connection in that same thread when the handler returns (a yield
    dependency's teardown would run in another thread). Async handlers run on
    the event-loop thread and keep its one connection.
    """

    def __init__(self, path, endpoint, **kwargs):
        if not inspect.iscoroutinefunction(endpoint):
            endpoint = closes_connection(endpoint)
        super().__init__(path, endpoint, **kwargs)
