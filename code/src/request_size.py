# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""A cap on request body size, enforced before anything reads the body.

Only a multipart upload (an input file) may be large. Any other body (JSON,
a urlencoded form) gets a small cap: FastAPI parses a JSON body before the
route's dependencies run, so without it an unauthenticated client could make
the API parse a body of any size, e.g. on the login route.

Starlette parses (and spools to disk) a multipart form before the route's
dependencies run, so without a cap an unauthenticated client could make the
API store a body of any size before authentication rejected it. A declared
Content-Length over the cap is answered 413 at once. A body without one
(chunked) is counted as it is received and cut off with 413 once over the cap.
"""
import json
import os

from fastapi import HTTPException

from constants import MAX_UPLOAD_BYTES


# The largest input file plus room for the multipart framing and the form's
# other fields (description, job parameters).
MAX_REQUEST_BYTES = int(os.environ.get('NBODY_MAX_REQUEST_BYTES', MAX_UPLOAD_BYTES + 2 * 2**20))
# Every other body: the largest is a comparison request or a job's description
MAX_OTHER_REQUEST_BYTES = int(os.environ.get('NBODY_MAX_OTHER_REQUEST_BYTES', 64 * 2**10))


class RequestTooLarge(HTTPException):
    """Raised from receive() once a streamed body passes the cap. An
    HTTPException, because FastAPI turns any other error raised while it
    parses a form into a 400. This one it re-raises, and answers 413."""

    def __init__(self, max_bytes: int):
        super().__init__(status_code=413, detail=_detail(max_bytes))


def _detail(max_bytes: int) -> str:
    size = f"{max_bytes // 2**20} MB" if max_bytes >= 2**20 else f"{max_bytes // 2**10} KB"
    return f"Request body too large (max {size})"


class RequestSizeLimitMiddleware:
    """Pure ASGI middleware: 413 for an HTTP request body over max_bytes
    (multipart/form-data) or max_other_bytes (any other content type).
    Responses (streaming downloads included) pass through untouched."""

    def __init__(self, app, max_bytes: int = MAX_REQUEST_BYTES,
                 max_other_bytes: int = MAX_OTHER_REQUEST_BYTES):
        self.app = app
        self.max_bytes = max_bytes
        self.max_other_bytes = max_other_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        declared = None
        limit = self.max_other_bytes
        for name, value in scope.get('headers') or ():
            if name == b'content-length':
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
            elif name == b'content-type' and value.lower().startswith(b'multipart/form-data'):
                limit = self.max_bytes
        if declared is not None and declared > limit:
            return await self._reject(send, limit)

        received = 0
        response_started = False

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message['type'] == 'http.request':
                received += len(message.get('body', b''))
                if received > limit:
                    raise RequestTooLarge(limit)
            return message

        async def tracking_send(message):
            nonlocal response_started
            if message['type'] == 'http.response.start':
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except RequestTooLarge:
            if not response_started:
                await self._reject(send, limit)

    async def _reject(self, send, limit):
        body = json.dumps({'detail': _detail(limit)}).encode()
        await send({'type': 'http.response.start', 'status': 413,
                    'headers': [(b'content-type', b'application/json'),
                                (b'content-length', str(len(body)).encode()),
                                (b'connection', b'close')]})
        await send({'type': 'http.response.body', 'body': body})
