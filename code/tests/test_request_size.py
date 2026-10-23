# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The request body cap (request_size): Starlette spools a multipart form
before authentication runs, so an unauthenticated client could make the API
store a body of any size. Over the cap, a request is answered 413 without the
route running, whether it declares its length or streams it chunked."""
import pytest
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

import api
from constants import MAX_UPLOAD_BYTES
from request_size import MAX_OTHER_REQUEST_BYTES, MAX_REQUEST_BYTES, RequestSizeLimitMiddleware


LIMIT = 4096
OTHER_LIMIT = 1024


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=LIMIT, max_other_bytes=OTHER_LIMIT)
    app.state.calls = []

    @app.post("/json")
    async def json_body(body: dict):
        app.state.calls.append("json")
        return {"keys": len(body)}

    @app.post("/upload")
    async def upload(file: UploadFile = File(...), description: str = Form("")):
        app.state.calls.append(file.filename)
        return {"size": len(await file.read())}

    @app.get("/download")
    def download():
        return {"data": "x" * (4 * LIMIT)}         # responses are not capped

    with TestClient(app) as c:
        c.calls = app.state.calls
        yield c


def test_a_normal_upload_passes(client):
    r = client.post("/upload", files={"file": ("b.csv", b"n,idBody\n" * 10)}, data={"description": "d"})
    assert r.status_code == 200 and r.json() == {"size": 90}
    assert client.get("/download").status_code == 200


def test_a_declared_oversize_body_is_413_before_the_route_runs(client):
    r = client.post("/upload", files={"file": ("b.csv", b"x" * (2 * LIMIT))})
    assert r.status_code == 413 and "too large" in r.json()["detail"]
    assert client.calls == []


def test_a_chunked_oversize_body_is_cut_off_with_413(client):
    def chunks():                                  # no Content-Length: Transfer-Encoding chunked
        for _ in range(16):
            yield b"x" * 512
    r = client.post("/upload", content=chunks(),
                    headers={"content-type": "multipart/form-data; boundary=zz"})
    assert r.status_code == 413
    assert client.calls == []


def test_a_json_body_gets_the_small_cap(client):
    assert client.post("/json", json={"a": 1}).status_code == 200
    # under the multipart cap, over the one for other bodies
    r = client.post("/json", json={"a": "x" * (2 * OTHER_LIMIT)})
    assert r.status_code == 413 and "KB" in r.json()["detail"]
    assert client.calls == ["json"]


def test_a_large_json_login_is_413_on_the_api():
    # no lifespan (no database): the cap answers before the route runs
    r = TestClient(api.app).post("/api/auth/login", content=b'{"username": "' + b"x" * 2**20 + b'"}',
                                 headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_the_api_installs_the_cap_inside_cors():
    classes = [m.cls for m in api.app.user_middleware]
    assert RequestSizeLimitMiddleware in classes
    # user_middleware lists the outermost first: CORS wraps the cap
    assert classes.index(CORSMiddleware) < classes.index(RequestSizeLimitMiddleware)
    assert MAX_REQUEST_BYTES > MAX_UPLOAD_BYTES
    assert MAX_OTHER_REQUEST_BYTES < 2**20
