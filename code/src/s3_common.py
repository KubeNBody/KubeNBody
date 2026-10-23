# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""What every S3/MinIO client of the code shares (the synchronous v1 and v2
services and the async one): endpoint, credentials, TLS verification,
timeouts, bucket names, and creating a missing bucket."""

from botocore.config import Config
from botocore.exceptions import ClientError

from config import settings


PARTIAL_RESULTS_BUCKET = settings.s3.partial_results_bucket
EPOCH_RESULTS_BUCKET = settings.s3.epoch_results_bucket
INPUT_FILES_BUCKET = settings.s3.input_files_bucket


def client_config(**extra) -> Config:
    """botocore Config with the configured timeouts (extra: e.g. retries,
    max_pool_connections)."""
    cfg = settings.s3
    return Config(signature_version='s3v4', connect_timeout=cfg.connect_timeout,
                  read_timeout=cfg.read_timeout, **extra)


def client_kwargs() -> dict:
    """Endpoint, credentials and TLS verification for boto3/aioboto3 client()
    and resource()."""
    cfg = settings.s3
    return dict(endpoint_url=cfg.endpoint_url, aws_access_key_id=cfg.access_key,
                aws_secret_access_key=cfg.secret_key, verify=cfg.verify_ssl)


def ensure_bucket(client, bucket_name: str) -> bool:
    """Create the bucket if it does not exist. True if it was created. Any
    other error of the check (denied, unreachable) raises."""
    try:
        client.head_bucket(Bucket=bucket_name)
        return False
    except ClientError as e:
        if str(e.response.get('Error', {}).get('Code')) not in ('404', 'NoSuchBucket', 'NotFound'):
            raise
    try:
        client.create_bucket(Bucket=bucket_name)
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') not in ('BucketAlreadyOwnedByYou', 'BucketAlreadyExists'):
            raise
        return False
    return True
