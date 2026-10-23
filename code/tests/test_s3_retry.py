# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""with_s3_retry retries throttling and server faults, not permanent errors:
a missing key or a denied request fails the same way on every attempt, and
retrying it only added seconds of sleep to every "file may be absent" path."""
from __future__ import annotations

import types

import pytest
from botocore.exceptions import ClientError

import file_service as F
from file_service import with_s3_retry
from helpers import patch_where_used


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")


@pytest.mark.parametrize("code", ["NoSuchKey", "404", "403", "AccessDenied"])
def test_permanent_errors_are_not_retried(code):
    calls = []

    @with_s3_retry(max_retries=3, base_delay=0.001)
    def get():
        calls.append(1)
        raise _err(code)
    with pytest.raises(ClientError):
        get()
    assert len(calls) == 1


def test_throttling_is_retried():
    calls = []

    @with_s3_retry(max_retries=3, base_delay=0.001)
    def get():
        calls.append(1)
        if len(calls) < 3:
            raise _err("SlowDown")
        return "ok"
    assert get() == "ok" and len(calls) == 3


def _combine(monkeypatch, failures):
    """Run combine_epoch_snapshots over 3 epochs whose downloads raise
    failures[epoch]. Return the uploaded parts' text and the S3 calls."""
    calls, parts = [], []

    class Client:
        def create_multipart_upload(self, **k):
            return {"UploadId": "u"}

        def upload_part(self, **k):
            parts.append(k["Body"].decode())
            return {"ETag": "e"}

        def complete_multipart_upload(self, **k):
            calls.append("complete")

        def abort_multipart_upload(self, **k):
            calls.append("abort")
    patch_where_used(monkeypatch, F, "get_s3", lambda: types.SimpleNamespace(meta=types.SimpleNamespace(client=Client())))

    def download(bucket, key):
        epoch = int(key.split("/")[3])
        if epoch in failures:
            raise failures[epoch]
        return f"h\n{epoch}\n"
    patch_where_used(monkeypatch, F, "download_file", download)
    monkeypatch.setattr(F, "generate_animation_file_from_s3", lambda *a: None)
    F.combine_epoch_snapshots(job_id=1, snapshot_interval=1, num_epochs=3)
    return "".join(parts), calls


def test_combine_skips_a_snapshot_that_does_not_exist(monkeypatch):
    text, calls = _combine(monkeypatch, {2: _err("NoSuchKey")})
    assert text == "h\n1\n3\n" and calls == ["complete"]


def test_combine_fails_on_a_transient_error_instead_of_dropping_the_epoch(monkeypatch):
    """The per-epoch files are deleted once the combined file exists: an epoch
    skipped after a transient error was lost for good."""
    with pytest.raises(ClientError):
        _combine(monkeypatch, {2: _err("SlowDown")})
