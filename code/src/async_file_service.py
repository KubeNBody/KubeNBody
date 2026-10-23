# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Async file service for S3/MinIO operations using aioboto3.
"""
import asyncio
import contextlib
import logging
import os
import tempfile
from typing import List, Optional, Tuple

import aioboto3

import s3_common
from transient_errors import is_missing_object


logger = logging.getLogger(__name__)

PARTIAL_RESULTS_BUCKET_NAME = s3_common.PARTIAL_RESULTS_BUCKET
EPOCH_RESULTS_BUCKET_NAME = s3_common.EPOCH_RESULTS_BUCKET


class AsyncS3Service:
    """Async S3/MinIO service for file operations.

    Each call opens its own client unless the service is entered as a context
    manager (`async with AsyncS3Service() as s3:`), when all calls share one:
    use that for batches (a client per object meant a connection pool and a
    TLS handshake per file). Clients get the timeouts and TLS verification of
    the synchronous service (file_service).
    """

    MAX_POOL_CONNECTIONS = 32

    def __init__(self):
        self._config = s3_common.client_config(max_pool_connections=self.MAX_POOL_CONNECTIONS)
        self.session = aioboto3.Session()
        self._shared = None
        self._shared_cm = None

    def _new_client(self):
        return self.session.client('s3', config=self._config, **s3_common.client_kwargs())

    async def __aenter__(self):
        self._shared_cm = self._new_client()
        self._shared = await self._shared_cm.__aenter__()
        return self

    async def __aexit__(self, *exc):
        cm, self._shared, self._shared_cm = self._shared_cm, None, None
        await cm.__aexit__(*exc)

    @contextlib.asynccontextmanager
    async def _s3(self):
        if self._shared is not None:
            yield self._shared
        else:
            async with self._new_client() as client:
                yield client

    async def download_file(self, bucket: str, key: str) -> str:
        """Download a file from S3/MinIO asynchronously."""
        async with self._s3() as s3_client:
            try:
                response = await s3_client.get_object(Bucket=bucket, Key=key)
                content = await response['Body'].read()
                return content.decode('utf-8')
            except Exception as e:
                logger.error(f"Failed to download {bucket}/{key}: {e}")
                raise

    async def stream_to_tempfile(self, bucket: str, key: str, chunk_size: int = 64 * 1024) -> str:
        """Stream an S3 object to a temp file on disk and return its path.

        Memory-efficient alternative to `download_file` for large objects
        (e.g. combined-results CSVs that can be hundreds of MiB). The body is
        written to disk in `chunk_size` increments rather than buffered in
        RAM. Caller is responsible for unlinking the returned path.
        """
        fd, path = tempfile.mkstemp(prefix='nbody-stream-', suffix='.bin')
        try:
            async with self._s3() as s3_client:
                response = await s3_client.get_object(Bucket=bucket, Key=key)
                with os.fdopen(fd, 'wb') as f:
                    fd = None  # ownership transferred to the file object
                    async for chunk in response['Body'].iter_chunks(chunk_size=chunk_size):
                        f.write(chunk)
            return path
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if os.path.exists(path):
                os.unlink(path)
            raise

    async def upload_file(self, bucket: str, key: str, file_content: str) -> bool:
        """Upload a file to S3/MinIO asynchronously."""
        async with self._s3() as s3_client:
            try:
                await s3_client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=file_content.encode('utf-8') if isinstance(file_content, str) else file_content
                )
                logger.debug(f"Uploaded to {bucket}/{key}")
                return True
            except Exception as e:
                logger.error(f"Failed to upload to {bucket}/{key}: {e}")
                raise

    async def download_files_batch(self, bucket: str, keys: List[str],
                                   raise_errors: bool = False) -> List[Tuple[str, Optional[str]]]:
        """(key, content) of each file, downloaded concurrently, content None for a
        failed download. With raise_errors, None only for a missing object, and any
        other error (an outage, a throttle) is raised."""
        async with contextlib.AsyncExitStack() as stack:
            if self._shared is None:            # one client for the whole batch
                await stack.enter_async_context(self)
            tasks = [self.download_file(bucket, key) for key in keys]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        if raise_errors:
            for content in results:
                if isinstance(content, Exception) and not is_missing_object(content):
                    raise content
        return [
            (key, content if not isinstance(content, Exception) else None)
            for key, content in zip(keys, results)
        ]

