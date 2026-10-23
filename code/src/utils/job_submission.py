# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""What the v1 (routes/jobs.py) and v2 (v2/api_v2.py) submit endpoints share:
the help of their common form fields, the checks made before any file work
(so a rejected submission leaves no library file behind), and the Job row's
common columns."""

import json

from fastapi import HTTPException
from starlette.status import HTTP_400_BAD_REQUEST

from api_schemas import normalize_job_name
from config import settings
from schemas import JobStatus
from utils.input_files import ResolvedInput
from utils.job_helpers import get_barnes_hut_theta, validate_force_algorithm, validate_integration_method
from utils.metadata import collect_metadata


FORCE_ALGORITHM_HELP = ("Force calculation algorithm: 'direct' (exact O(N^2)) or 'barnes_hut' "
                        "(approximate O(N log N)). Barnes-Hut is recommended for N > 1000 bodies.")
THETA_HELP = ("Barnes-Hut opening angle (theta). Lower = more accurate but slower. Only used when "
              "force_algorithm='barnes_hut'. Typical values: 0.3 (accurate), 0.5 (balanced), 0.7-1.0 (fast).")
SOFTENING_HELP = "Gravitational softening parameter (epsilon squared)"
ADAPTIVE_HELP = "Enable adaptive timestepping (softening-length criterion, dt = eta*sqrt(eps/a_max))"
ADAPTIVE_ETA_HELP = "Safety factor for the adaptive timestep (smaller = more conservative)"
ADAPTIVE_DT_MIN_HELP = "Minimum timestep (0 = the default 1e-10)"
ADAPTIVE_DT_MAX_HELP = "Maximum timestep (0 = the time_step value)"
JOB_DESCRIPTION_HELP = "Optional user note for this job (distinct from file description)"
JOB_NAME_HELP = "Optional short title shown instead of the input file name"


def validate_job_parameters(num_epochs: int, snapshot_interval: int, force_algorithm: str,
                            integration_method: str, use_gpu: bool) -> None:
    """400 for a snapshot interval over the epoch count or an algorithm or
    integrator the device does not support."""
    if snapshot_interval > num_epochs:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"snapshot_interval ({snapshot_interval}) cannot exceed num_epochs ({num_epochs})"
        )
    validate_force_algorithm(force_algorithm, use_gpu)
    validate_integration_method(integration_method, use_gpu)


def validate_adaptive_bounds(adaptive_dt_min: float, adaptive_dt_max: float) -> None:
    """400 for a minimum timestep over the maximum (0 is no bound)."""
    if adaptive_dt_min > 0 and adaptive_dt_max > 0 and adaptive_dt_min > adaptive_dt_max:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"adaptive_dt_min ({adaptive_dt_min}) cannot exceed adaptive_dt_max ({adaptive_dt_max})"
        )


def job_fields(source: ResolvedInput, *, num_epochs: int, time_step: float, snapshot_interval: int,
               use_gpu: bool, force_algorithm: str, barnes_hut_theta: float, softening_sq: float,
               integration_method: str, adaptive_enabled: bool, adaptive_eta: float,
               adaptive_dt_min: float, adaptive_dt_max: float, job_description, job_name) -> dict:
    """The Job columns both endpoints set, for a new IN_PROGRESS job. A dt
    bound of 0 is stored as None (no bound)."""
    return dict(
        num_epochs=num_epochs,
        input_file_key=source.file_key,
        input_bucket=settings.s3.input_files_bucket,
        input_file_id=source.file_id,
        status=JobStatus.IN_PROGRESS.value,
        time_step=time_step,
        snapshot_interval=snapshot_interval,
        filename=source.filename,
        file_size=source.file_size,
        gpu_enabled=use_gpu,
        force_algorithm=force_algorithm,
        barnes_hut_theta=get_barnes_hut_theta(force_algorithm, barnes_hut_theta),
        softening_sq=softening_sq,
        integration_method=integration_method,
        adaptive_enabled=adaptive_enabled,
        adaptive_eta=adaptive_eta,
        adaptive_dt_min=adaptive_dt_min if adaptive_dt_min > 0 else None,
        adaptive_dt_max=adaptive_dt_max if adaptive_dt_max > 0 else None,
        metadata_json=json.dumps(collect_metadata()),
        description=job_description,
        name=normalize_job_name(job_name),
    )
