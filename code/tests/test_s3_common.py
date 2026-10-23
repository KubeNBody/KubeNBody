# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The settings every S3 client is built from, and creating a missing bucket."""
import pytest
from botocore.exceptions import ClientError

import s3_common
from config import settings


def _error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "HeadBucket")


class _Client:
    def __init__(self, head=None, create=None):
        self.head, self.create, self.created = head, create, []

    def head_bucket(self, Bucket):
        if self.head:
            raise _error(self.head)

    def create_bucket(self, Bucket):
        if self.create:
            raise _error(self.create)
        self.created.append(Bucket)


@pytest.mark.parametrize("head,create,expected", [
    (None, None, False),                        # exists
    ("404", None, True),                        # missing: created
    ("NoSuchBucket", None, True),
    ("404", "BucketAlreadyOwnedByYou", False),  # created meanwhile by another service
])
def test_ensure_bucket(head, create, expected):
    assert s3_common.ensure_bucket(_Client(head, create), "b") is expected


def test_ensure_bucket_raises_on_anything_but_a_missing_bucket():
    with pytest.raises(ClientError):
        s3_common.ensure_bucket(_Client(head="403"), "b")


def test_every_client_gets_the_configured_endpoint_tls_and_timeouts():
    kw, cfg = s3_common.client_kwargs(), s3_common.client_config(retries={"max_attempts": 3})
    assert kw["endpoint_url"] == settings.s3.endpoint_url and kw["verify"] == settings.s3.verify_ssl
    assert cfg.connect_timeout == settings.s3.connect_timeout and cfg.read_timeout == settings.s3.read_timeout
