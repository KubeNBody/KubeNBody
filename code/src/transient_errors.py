# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Telling an infrastructure outage apart from a task's own fault, for the
workers' retry budgets: an outage is waited out, not counted against the job."""
import peewee
import psycopg2
from botocore.exceptions import ClientError, EndpointConnectionError, HTTPClientError, IncompleteReadError
from botocore.exceptions import ConnectionError as BotoConnectionError
from psycopg2.extensions import QueryCanceledError
from urllib3.exceptions import HTTPError


# S3 errors that are an outage (connection refused or dropped, a timeout, a
# truncated response). The rest of BotoCoreError - no credentials, no region,
# invalid parameters - is a configuration fault that fails the same way every
# time.
S3_OUTAGE_EXCEPTIONS = (
    EndpointConnectionError,
    BotoConnectionError,
    HTTPClientError,
    IncompleteReadError,
    HTTPError,
    ConnectionError,
    TimeoutError,
)

# Client errors worth retrying: throttling and server-side faults. A missing
# key, a denied request or a bad request fails the same way every time.
S3_RETRYABLE_ERROR_CODES = frozenset({
    'SlowDown', 'InternalError', 'ServiceUnavailable', 'RequestTimeout',
    'RequestTimeTooSkewed', 'Throttling', 'ThrottlingException', '500', '502', '503', '504',
})


def is_retryable_s3_error(exc: Exception) -> bool:
    if isinstance(exc, ClientError):
        return str(exc.response.get('Error', {}).get('Code', '')) in S3_RETRYABLE_ERROR_CODES
    return isinstance(exc, S3_OUTAGE_EXCEPTIONS)


def _chain(exc: BaseException):
    """exc and the exceptions it wraps (peewee re-raises the driver's error
    as its own, leaving the original as the context)."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _is_permanent_db_error(exc: Exception) -> bool:
    """Database errors that look like an outage but repeat on every attempt:
    a statement cancelled by its timeout, a database never initialised."""
    for e in _chain(exc):
        if isinstance(e, QueryCanceledError):
            return True
        if 'must be initialized' in str(e):
            return True
    return False


def is_missing_object(exc: Exception) -> bool:
    """Whether an S3 read failed because the object does not exist."""
    return (isinstance(exc, ClientError)
            and str(exc.response.get('Error', {}).get('Code', '')) in ('NoSuchKey', '404', 'NotFound'))


def is_transient_error(exc: Exception) -> bool:
    """An infrastructure outage rather than the task's fault: a retryable S3
    error (throttling, a server fault, a refused or dropped connection, a
    timeout), or a lost/unreachable PostgreSQL connection (peewee wraps the
    driver's errors, psycopg2's are checked too, should one surface unwrapped).
    Configuration faults - missing S3 credentials or region, an uninitialised
    database - and statement timeouts are not: they fail the same way every
    time, so they count against the job like any other error."""
    if is_retryable_s3_error(exc):
        return True
    db_errors = (peewee.OperationalError, peewee.InterfaceError,
                 psycopg2.OperationalError, psycopg2.InterfaceError)
    return isinstance(exc, db_errors) and not _is_permanent_db_error(exc)
