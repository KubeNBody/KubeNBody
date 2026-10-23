# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Pydantic models of the API's requests and responses."""
from datetime import datetime
from typing import List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from constants import JOB_NAME_MAX, SOFTENING_SQ
from enums import JobStatus, PackageStatus  # noqa: F401 -- used in response models


INT32_MIN, INT32_MAX = -2**31, 2**31 - 1


class JobDescriptionUpdate(BaseModel):
    """Request body for PATCH /api/jobs/{id}/description."""
    description: Optional[str] = Field(None, max_length=1000)


class JobNameUpdate(BaseModel):
    """Request body for PATCH /api/jobs/{id}/name."""
    name: Optional[str] = Field(None, max_length=JOB_NAME_MAX)


def normalize_job_name(name: Optional[str]) -> Optional[str]:
    """A job name as stored: on one line, trimmed, None when blank."""
    if name is None:
        return None
    return " ".join(name.split()) or None


class JobResponse(BaseModel):
    """Response model for job details."""
    job_id: int
    status: JobStatus
    num_epochs: int
    current_epoch: int
    package_size: int
    num_packages: int
    input_file_key: str
    results_file_key: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None    # first worker started computing
    completed_at: Optional[datetime] = None  # job finalized
    retry_count: Optional[int] = None        # monolithic retry budget (reset on progress)
    recovery_count: Optional[int] = None     # resumes after an interrupted run, never reset
    lease_owner: Optional[str] = None        # monolithic: worker_id holding the lease (<pod>-<pid>-<CPU|GPU>)
    progress_percentage: float = Field(..., ge=0, le=100)
    snapshot_interval: int = 5
    time_step: float
    filename: Optional[str] = None
    file_size: Optional[int] = None
    job_type: str = "distributed"
    gpu_enabled: bool = False
    # Algorithm selection fields
    force_algorithm: str = "direct"  # 'direct' or 'barnes_hut'
    barnes_hut_theta: float = 0.5  # Opening angle for Barnes-Hut
    softening_sq: float = SOFTENING_SQ  # Gravitational softening (epsilon squared)
    # Integration method selection
    integration_method: str = "symplectic_euler"  # an integrators.IntegrationMethod value
    # Adaptive timestepping
    adaptive_enabled: bool = False
    current_simulation_time: float = 0.0
    metadata: Optional[dict] = None
    description: Optional[str] = None  # User-supplied note for this job
    name: Optional[str] = None  # User-supplied short title for this job
    # Package partitioning (V2 distributed): 'linear' (file order), 'random'
    # (seeded shuffle), 'spatial' (Morton/Z-order) or 'orb' (recursive bisection)
    partition_strategy: str = "linear"
    partition_seed: int = 0

    model_config = ConfigDict(from_attributes=True)


class JobListResponse(BaseModel):
    """Response model for job listing."""
    jobs: List[JobResponse]
    total: int
    page: int
    page_size: int
    pages: int


class JobSelectableResponse(BaseModel):
    """Minimal job fields for selection dropdowns (compare page, etc).

    Returned unpaginated by /api/jobs/selectable so the UI can list ALL jobs
    without hitting page_size limits. No package joins / heavy fields.
    """
    job_id: int
    status: JobStatus
    job_type: str = "distributed"
    filename: Optional[str] = None
    num_epochs: int
    time_step: float
    gpu_enabled: bool = False
    force_algorithm: str = "direct"
    integration_method: str = "symplectic_euler"
    softening_sq: float = SOFTENING_SQ  # Gravitational softening (epsilon squared)
    description: Optional[str] = None  # User-supplied note for this job
    name: Optional[str] = None  # User-supplied short title for this job
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class JobProgressResponse(BaseModel):
    """Response model for job progress."""
    job_id: int
    status: JobStatus
    current_epoch: int
    total_epochs: int
    packages_completed: int
    total_packages: int
    progress_percentage: float
    estimated_time_remaining: Optional[int] = None  # seconds
    force_algorithm: str = "direct"  # 'direct' or 'barnes_hut'
    barnes_hut_theta: float = 0.5  # Opening angle for Barnes-Hut
    softening_sq: float = SOFTENING_SQ  # Gravitational softening (epsilon squared)
    integration_method: str = "symplectic_euler"  # an integrators.IntegrationMethod value


class PackageResponse(BaseModel):
    """Response model for package details."""
    package_id: int
    package_number: int
    epoch_num: int
    status: PackageStatus
    file_key: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    """Response model for health check."""
    status: str
    services: dict
    timestamp: datetime


class MetricsResponse(BaseModel):
    """Response model for system metrics."""
    active_jobs: int
    completed_jobs: int
    failed_jobs: int
    total_packages_processed: int
    queue_sizes: dict
    timestamp: datetime


class FileUploadResponse(BaseModel):
    """Response model for file upload."""
    job_id: int
    message: str
    file_key: str
    num_bodies: int


class BodyData(BaseModel):
    """Model for validating body data in CSV."""
    # NaN/Inf coordinates are refused here as on the V2 upload path. The
    # mass check below already refuses a NaN mass
    model_config = ConfigDict(allow_inf_nan=False)

    # The workers keep ids in int32: a larger id would wrap and could equal
    # another body's, whose pair force is then skipped as a self-interaction
    n: int = Field(..., ge=INT32_MIN, le=INT32_MAX)
    idBody: int = Field(..., ge=INT32_MIN, le=INT32_MAX)
    m: float = Field(..., gt=0, description="Mass must be positive")
    x: float
    y: float
    z: float
    vx: float
    vy: float
    vz: float

    @field_validator('m')
    @classmethod
    def validate_mass(cls, v):
        if v <= 0:
            raise ValueError('Mass must be positive')
        return v


class AnimationSnapshot(BaseModel):
    """Single snapshot for animation."""
    epoch: int
    particles: List[List[float]] = Field(..., description="Array of [x, y, z, mass] arrays")
    center_of_mass: Tuple[float, float, float] = Field(..., description="Center of mass [x, y, z] for this snapshot")


class PaginatedAnimationDataResponse(BaseModel):
    """Paginated response model for animation data."""
    job_id: int
    snapshot_interval: int
    total_epochs: int
    total_snapshots: int
    offset: int
    limit: int
    has_next: bool
    snapshots: List[AnimationSnapshot]


class SnapshotInfo(BaseModel):
    """Information about a single snapshot."""
    epoch: int
    file_key: str


class SnapshotsListResponse(BaseModel):
    """Response model for listing available snapshots."""
    job_id: int
    snapshot_interval: int
    current_epoch: int
    snapshots: List[SnapshotInfo]


# Input File Library Models

class InputFileInfo(BaseModel):
    """Information about an input file in the file library."""
    file_id: int
    user_friendly_name: str
    original_filename: str
    num_bodies: int
    file_size: int
    created_at: str
    last_used_at: str
    usage_count: int
    description: Optional[str] = None


class InputFileListResponse(BaseModel):
    """Response model for listing input files."""
    files: List[InputFileInfo]
    total: int
    page: int = 1
    page_size: int = 20


class FileNameCheckResponse(BaseModel):
    """Response for checking if a file name is available."""
    exists: bool
    file_id: Optional[int] = None
    user_friendly_name: Optional[str] = None
    num_bodies: Optional[int] = None
    file_size: Optional[int] = None
    created_at: Optional[str] = None
    usage_count: Optional[int] = None


# Simulation Comparison Models

class EpochMetricsResponse(BaseModel):
    """Conservation metrics at a single epoch."""
    epoch: int
    simulation_time: float
    kinetic_energy: float
    potential_energy: float
    total_energy: float
    relative_energy_error: float  # (E - E0) / |E0|
    momentum: Tuple[float, float, float]
    momentum_magnitude: float
    angular_momentum: Tuple[float, float, float]
    angular_momentum_magnitude: float


class JobMetricsResponse(BaseModel):
    """Complete metrics result for a job."""
    job_id: int
    epochs: List[int]
    simulation_times: List[float]
    metrics: List[EpochMetricsResponse]
    energy_drift_percent: float
    momentum_conserved: bool  # True if |P| stays within 1% of initial
    angular_momentum_conserved: bool
    computation_time_ms: int
    # False: an adaptive job's simulation times were not all recorded, and
    # estimates from the recorded times stand in for the missing ones
    simulation_times_exact: bool = True
    # snapshots the job should have that do not exist (left out of the metrics)
    snapshots_missing: int = 0


class JobComparisonSummary(BaseModel):
    """Summary info about a job for comparison."""
    job_id: int
    status: str
    job_type: str
    num_epochs: int
    current_epoch: int
    snapshot_interval: int
    force_algorithm: str
    barnes_hut_theta: Optional[float] = None
    softening_sq: float = SOFTENING_SQ
    integration_method: str
    time_step: float
    gpu_enabled: bool
    filename: Optional[str] = None
    description: Optional[str] = None  # User-supplied note for this job
    name: Optional[str] = None  # User-supplied short title for this job


class ComparisonCacheStatsResponse(BaseModel):
    """Cache statistics for monitoring."""
    metrics_cache_size: int
    metrics_ttl_seconds: int


# Job-to-Job Comparison Models

class JobToJobComparisonRequest(BaseModel):
    """Request for comparing jobs against a reference."""
    reference_job_id: int = Field(..., description="Reference job ID (ground truth)")
    comparison_job_ids: List[int] = Field(..., min_length=1, max_length=5, description="Job IDs to compare "
                                                                                       "against reference")
    max_snapshots: Optional[int] = Field(default=None, ge=1, description="Maximum snapshots to compare (None = all)")


class SingleJobComparisonResult(BaseModel):
    """Comparison result for one job against the reference."""
    job_id: int
    job_info: JobComparisonSummary

    # Trajectory divergence summary
    epochs_compared: int
    position_rmse_final: float
    velocity_rmse_final: float

    # Energy comparison
    energy_drift_reference: float  # Reference job's energy drift %
    energy_drift_comparison: float  # This job's energy drift %

    # Momentum comparison: |P - P0| as % of sum m|v| at t = 0
    momentum_drift_reference: float
    momentum_drift_comparison: float

    # Defaults: results stored before these were recorded
    final_time_reference: Optional[float] = None   # time of each job's last snapshot,
    final_time_comparison: Optional[float] = None  # where its drifts are measured
    interpolated_points: int = 0         # points where the comparison job's state was interpolated
    simulation_times_exact: bool = True  # False: an adaptive job's time axis is partly estimated
    snapshots_missing_reference: int = 0
    snapshots_missing_comparison: int = 0
    energy_initial_reference: Optional[float] = None   # E0 of the drifts above
    energy_initial_comparison: Optional[float] = None

    # Timeseries (for charts)
    epochs: List[int]
    simulation_times: List[float]
    position_rmse_timeseries: List[float]
    velocity_rmse_timeseries: List[float]
    energy_reference_timeseries: List[float]
    energy_comparison_timeseries: List[float]
    momentum_reference_timeseries: List[float]
    momentum_comparison_timeseries: List[float]


class SkippedJobInfo(BaseModel):
    """Info about a job that was skipped during comparison."""
    job_id: int
    reason: str


class ComparisonJobSubmitRequest(BaseModel):
    """Request to create a persistent comparison job."""
    reference_job_id: int
    comparison_job_ids: List[int] = Field(..., min_length=1, max_length=10)
    max_snapshots: Optional[int] = Field(default=None, ge=1, description="Maximum snapshots (None = all)")

class ComparisonJobResponse(BaseModel):
    """Response for a persistent comparison job."""
    comparison_id: int
    status: str
    reference_job_id: int
    comparison_job_ids: List[int]
    # None = all snapshots, as in ComparisonJobSubmitRequest
    max_snapshots: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    computation_time_ms: Optional[int] = None
    jobs_compared: int = 0
    jobs_skipped: int = 0
    error_message: Optional[str] = None
    stale: bool = False
    stale_reason: Optional[str] = None
    results: Optional['JobToJobComparisonResponse'] = None

class ComparisonJobListResponse(BaseModel):
    """Paginated list of comparison jobs."""
    comparisons: List[ComparisonJobResponse]
    total: int
    page: int
    page_size: int
    pages: int


class JobToJobComparisonResponse(BaseModel):
    """Complete response for job-to-job comparison."""
    reference_job_id: int
    reference_job_info: JobComparisonSummary
    comparisons: List[SingleJobComparisonResult]
    skipped_jobs: List[SkippedJobInfo] = []
    computation_time_ms: int