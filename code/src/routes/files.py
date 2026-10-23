# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Endpoints for managing input files (upload, list, download, delete).
"""

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, status
from fastapi.responses import StreamingResponse
from starlette.status import HTTP_404_NOT_FOUND, HTTP_500_INTERNAL_SERVER_ERROR

from api_schemas import FileNameCheckResponse, InputFileInfo, InputFileListResponse
from auth import get_current_user
from db_threads import ClosingRoute
from file_service import delete_file as delete_s3_file
from file_service import download_file
from logging_config import setup_logging
from models import InputFile, Job, User
from shared.validation import sanitize_file_name


logger = setup_logging('api.files')

def attachment_disposition(filename: str) -> str:
    """Content-Disposition for a download: an ASCII fallback name plus the
    UTF-8 name (RFC 6266 / RFC 5987): header values must be Latin-1, so a
    non-Latin name in the plain filename= parameter would fail the response."""
    fallback = (filename.encode('ascii', 'replace').decode('ascii')
                .replace('?', '_').replace('"', '_').replace('\\', '_')) or 'download'
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


router = APIRouter(prefix="/api/files", tags=["Files"], route_class=ClosingRoute)


def serialize_input_file(file: InputFile) -> InputFileInfo:
    """Convert InputFile model to API response."""
    return InputFileInfo(
        file_id=file.file_id,
        user_friendly_name=file.user_friendly_name,
        original_filename=file.original_filename,
        num_bodies=file.num_bodies,
        file_size=file.file_size,
        created_at=file.created_at.isoformat(),
        last_used_at=file.last_used_at.isoformat(),
        usage_count=file.usage_count,
        description=file.description
    )


@router.get("/check-name", response_model=FileNameCheckResponse)
def check_file_name(
    name: str = Query(..., min_length=1, max_length=255),
    current_user: User = Depends(get_current_user)
):
    """Check if a file name is already in use.

    Returns file details if exists, otherwise indicates name is available.
    """
    try:
        sanitized_name = sanitize_file_name(name)
        existing_file = InputFile.select().where(
            InputFile.user_friendly_name == sanitized_name
        ).first()

        if existing_file:
            return FileNameCheckResponse(
                exists=True,
                file_id=existing_file.file_id,
                user_friendly_name=existing_file.user_friendly_name,
                num_bodies=existing_file.num_bodies,
                file_size=existing_file.file_size,
                created_at=existing_file.created_at.isoformat(),
                usage_count=existing_file.usage_count
            )

        return FileNameCheckResponse(exists=False)

    except Exception as e:
        logger.error(f"Error checking file name: {e}")
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to check file name"
        )


@router.get("", response_model=InputFileListResponse)
def list_files(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    search: Optional[str] = Query(None, description="Search in name and description"),
    sort_by: str = Query("created_at", pattern="^(name|created_at|last_used_at|size)$"),
    current_user: User = Depends(get_current_user)
):
    """List all input files with pagination and search."""
    try:
        query = InputFile.select()

        if search:
            query = query.where(
                (InputFile.user_friendly_name.contains(search)) |
                (InputFile.description.contains(search))
            )

        total = query.count()

        if sort_by == "name":
            query = query.order_by(InputFile.user_friendly_name)
        elif sort_by == "last_used_at":
            query = query.order_by(InputFile.last_used_at.desc())
        elif sort_by == "size":
            query = query.order_by(InputFile.file_size.desc())
        else:
            query = query.order_by(InputFile.created_at.desc())

        offset = (page - 1) * page_size
        query = query.limit(page_size).offset(offset)

        files = list(query)

        return InputFileListResponse(
            files=[serialize_input_file(f) for f in files],
            total=total,
            page=page,
            page_size=page_size
        )

    except Exception as e:
        logger.error(f"Error listing files: {e}")
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list files"
        )


@router.get("/input", response_model=InputFileListResponse)
def list_input_files(current_user: User = Depends(get_current_user)):
    """List the input files of the file library (the InputFile table)."""
    try:
        files = InputFile.select().order_by(InputFile.last_used_at.desc())

        return InputFileListResponse(
            files=[serialize_input_file(f) for f in files],
            total=files.count(),
            page=1,
            page_size=files.count()
        )
    except Exception as e:
        logger.error(f"Failed to list input files: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to list input files")


@router.get("/{file_id}", response_model=InputFileInfo)
def get_file(
    file_id: int = Path(..., gt=0),
    current_user: User = Depends(get_current_user)
):
    """Get details of a specific input file."""
    try:
        file = InputFile.get_or_none(InputFile.file_id == file_id)

        if not file:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=f"File with id {file_id} not found"
            )

        return serialize_input_file(file)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting file {file_id}: {e}")
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get file details"
        )


@router.get("/{file_id}/download")
def download_input_file(
    file_id: int = Path(..., gt=0),
    current_user: User = Depends(get_current_user)
):
    """Download the actual file content for an input file."""
    try:
        file = InputFile.get_or_none(InputFile.file_id == file_id)

        if not file:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=f"File with id {file_id} not found"
            )

        file_content = download_file(file.s3_bucket, file.s3_key)

        disposition = attachment_disposition(file.original_filename)

        def iter_file():
            yield file_content.encode('utf-8')

        return StreamingResponse(
            iter_file(),
            media_type='text/csv',
            headers={
                "Content-Disposition": disposition,
                "Content-Length": str(len(file_content.encode('utf-8')))
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading file {file_id}: {e}")
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to download file"
        )


@router.delete("/{file_id}")
def delete_file(
    file_id: int = Path(..., gt=0),
    current_user: User = Depends(get_current_user)
):
    """Delete an input file that no job uses.

    Not only running jobs read their input: a paused job reads it again when
    resumed, and the comparisons of finished jobs load their initial state
    from it. Delete those jobs first."""
    try:
        file = InputFile.get_or_none(InputFile.file_id == file_id)

        if not file:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=f"File with id {file_id} not found"
            )

        # by id, or by key for older job rows that recorded no file id
        using_jobs = Job.select().where(
            (Job.input_file_id == file_id) | (Job.input_file_key == file.s3_key)
        ).count()

        if using_jobs > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot delete file: {using_jobs} job(s) use it (their comparisons and "
                       f"resumes read it), delete those jobs first"
            )

        try:
            delete_s3_file(file.s3_bucket, file.s3_key)
        except Exception as e:
            logger.warning(f"Failed to delete S3 file {file.s3_key}: {e}")

        file.delete_instance()

        return {"message": "File deleted successfully", "file_id": file_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting file {file_id}: {e}")
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete file"
        )


@router.patch("/{file_id}", response_model=InputFileInfo)
def update_file(
    file_id: int = Path(..., gt=0),
    user_friendly_name: Optional[str] = Form(None, max_length=255),
    description: Optional[str] = Form(None, max_length=1000),
    clear_description: bool = Form(False, description="Remove the description (an empty "
                                                      "description field reads as absent)"),
    current_user: User = Depends(get_current_user)
):
    """Update file name or description."""
    try:
        file = InputFile.get_or_none(InputFile.file_id == file_id)

        if not file:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=f"File with id {file_id} not found"
            )

        changes = {}
        if user_friendly_name is not None:
            new_name = sanitize_file_name(user_friendly_name)

            if new_name != file.user_friendly_name:
                conflict = InputFile.select().where(
                    (InputFile.user_friendly_name == new_name) &
                    (InputFile.file_id != file_id)
                ).first()

                if conflict:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"File name '{new_name}' is already in use"
                    )

                changes['user_friendly_name'] = new_name

        if clear_description:
            changes['description'] = None
        elif description is not None:
            # multipart form encoding sends line breaks as CRLF
            description = description.replace('\r\n', '\n').strip()
            changes['description'] = description or None

        # Only the edited columns, not file.save(): rewriting the row read here
        # would revert a concurrent submission's usage_count / last_used_at
        if changes:
            InputFile.update(**changes).where(InputFile.file_id == file_id).execute()
            file = InputFile.get_or_none(InputFile.file_id == file_id)
            if not file:
                raise HTTPException(
                    status_code=HTTP_404_NOT_FOUND,
                    detail=f"File with id {file_id} not found"
                )

        return serialize_input_file(file)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating file {file_id}: {e}")
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update file"
        )
