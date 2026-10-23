# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
The input file of a job submission, shared by the v1 (routes/jobs.py) and
v2 (v2/api_v2.py) submit endpoints: a new upload (stored as a library file)
or a library file by id.

The functions block (PostgreSQL, S3, parsing): the async v1 endpoint runs
resolve_input_file in a thread. Callers validate every job parameter first,
so a rejected submission leaves no library file behind.
"""

import io
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse
from peewee import IntegrityError

from api_schemas import BodyData
from config import settings
from constants import MAX_UPLOAD_BYTES
from file_service import download_file, upload_file
from logging_config import setup_logging
from models import InputFile, utcnow
from shared.validation import sanitize_file_name
from utils.file_parsing import decode_file_content, storage_csv, validate_csv_data


logger = setup_logging('utils.input_files')

INPUT_FILES_BUCKET = settings.s3.input_files_bucket


@dataclass
class ResolvedInput:
    """The bodies of a submission's input file and what the job records of it."""
    bodies: List[BodyData]
    file_key: str
    filename: str
    file_size: int
    file_id: Optional[int]       # the library file, if there is one


class NameConflict(Exception):
    """An upload's name is taken (and force_new was not set): the endpoint
    returns `response`, a 409 describing the existing file."""

    def __init__(self, existing):
        super().__init__(f"File name '{existing.user_friendly_name}' already exists")
        self.response = JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "name_conflict",
                "message": f"File name '{existing.user_friendly_name}' already exists",
                "existing_file": {
                    "file_id": existing.file_id,
                    "user_friendly_name": existing.user_friendly_name,
                    "num_bodies": existing.num_bodies,
                    "file_size": existing.file_size,
                    "created_at": existing.created_at.isoformat(),
                    "usage_count": existing.usage_count
                }
            }
        )


def record_file_use(file_id: int) -> None:
    """Count a submission's use of a library file, atomically in the database."""
    InputFile.update(usage_count=InputFile.usage_count + 1, last_used_at=utcnow()).where(
        InputFile.file_id == file_id).execute()


def check_file_source(file, file_id) -> None:
    """400 unless exactly one file source is given. 413 for an upload whose
    declared size is over MAX_UPLOAD_BYTES (before anything is read)."""
    if file is None and file_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide one of: 'file' (upload) or 'file_id' (library)"
        )
    if file is not None and file_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide only one file source: file or file_id"
        )
    if file is not None and (getattr(file, 'size', None) or 0) > MAX_UPLOAD_BYTES:
        raise _too_large()


def _too_large() -> HTTPException:
    return HTTPException(status_code=413,
                         detail=f"File too large (max {MAX_UPLOAD_BYTES // 2**20} MB)")


def resolve_input_file(file=None, file_id: Optional[int] = None,
                       user_friendly_name: Optional[str] = None, description: Optional[str] = None,
                       force_new: bool = False) -> ResolvedInput:
    """Load and validate the submission's input file (check_file_source has
    run), storing an upload as a new library file. Raises HTTPException
    (400/404/409/413) or NameConflict. Other errors are the caller's 500."""
    if file_id is not None:
        return _library_file(file_id)
    return _upload(file, user_friendly_name, description, force_new)


def _library_file(file_id: int) -> ResolvedInput:
    logger.info(f"Using file from library: file_id={file_id}")
    record = InputFile.get_or_none(InputFile.file_id == file_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"File with id {file_id} not found in library")
    try:
        bodies = validate_csv_data(download_file(record.s3_bucket, record.s3_key))
    except Exception as e:
        logger.error(f"Failed to load library file {file_id}: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Library file is invalid or corrupted: {str(e)}")
    # incremented in the database, so concurrent submissions all count
    record_file_use(record.file_id)
    logger.info(f"Using library file '{record.user_friendly_name}' with {len(bodies)} bodies")
    return ResolvedInput(bodies, record.s3_key, record.original_filename, record.file_size, record.file_id)


def _upload(file, user_friendly_name, description, force_new) -> ResolvedInput:
    logger.info(f"Uploading new file: {file.filename}")
    # one byte over the limit is enough to refuse it (when the client sent no size)
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise _too_large()
    try:
        file_str = decode_file_content(content, file.filename)
        bodies = validate_csv_data(file_str)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    del content
    logger.info(f"Validated {len(bodies)} bodies in uploaded file")
    file_str = storage_csv(file_str, bodies)   # canonical CSV for the workers
    file_size = len(file_str.encode('utf-8'))

    name = sanitize_file_name(user_friendly_name or Path(file.filename or '').stem)
    existing = InputFile.get_or_none(InputFile.user_friendly_name == name)
    if existing:
        if not force_new:
            raise NameConflict(existing)
        suffix = f"_{uuid.uuid4().hex[:8]}"
        name = name[:255 - len(suffix)] + suffix

    try:
        record = InputFile.create(
            user_friendly_name=name,
            original_filename=file.filename,
            s3_key=f"pending/{uuid.uuid4().hex}",     # unique until the upload below
            s3_bucket=INPUT_FILES_BUCKET,
            file_size=file_size,
            num_bodies=len(bodies),
            description=description.strip() if description and description.strip() else None,
            usage_count=1,
            created_at=utcnow(),
            last_used_at=utcnow()
        )
    except IntegrityError:
        # the same name, created by a concurrent submission since the check
        existing = InputFile.get_or_none(InputFile.user_friendly_name == name)
        if existing:
            raise NameConflict(existing)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"File name '{name}' already exists")

    file_key = f"files/{record.file_id}/{name}.csv"
    try:
        upload_file(INPUT_FILES_BUCKET, file_key, io.StringIO(file_str))
    except Exception:
        # no library entry pointing at an object that does not exist
        record.delete_instance()
        raise
    InputFile.update(s3_key=file_key).where(InputFile.file_id == record.file_id).execute()
    logger.info(f"Created library file '{name}' (id={record.file_id}) at {INPUT_FILES_BUCKET}/{file_key}")
    return ResolvedInput(bodies, file_key, file.filename, file_size, record.file_id)
