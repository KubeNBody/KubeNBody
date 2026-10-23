# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Task construction for one V2 epoch.

Shared by the orchestrator (job creation, resume, recovery) and the merging
worker (next epoch), so every path issues identical task payloads. Packages are
contiguous index ranges of package_size bodies. The last one may be shorter.
"""
import dataclasses
from dataclasses import dataclass
from typing import Any, Dict, List

from constants import DEFAULT_THETA, SOFTENING_SQ

from .schemas_v2 import TaskV2


@dataclass(frozen=True)
class EpochParams:
    """The job-level settings every task of a job carries (everything but the
    epoch, its step size and the generation)."""
    package_size: int
    total_packages: int
    num_epochs: int
    snapshot_interval: int
    force_algorithm: str
    barnes_hut_theta: float
    softening_sq: float
    integration_method: str
    adaptive_enabled: bool
    adaptive_eta: float
    adaptive_dt_min: float
    adaptive_dt_max: float
    gpu_enabled: bool

    @classmethod
    def from_meta(cls, meta: Dict[str, str]) -> 'EpochParams':
        """Parse the Redis job meta hash. Job creation (and a meta rebuilt from
        PostgreSQL) writes every field. The defaults, for a meta written before
        a field existed, are the values creation would have written."""
        return cls(
            package_size=int(meta['package_size']),
            total_packages=int(meta['total_packages']),
            num_epochs=int(meta['num_epochs']),
            snapshot_interval=int(meta.get('snapshot_interval', 0)),
            force_algorithm=meta.get('force_algorithm', 'direct'),
            barnes_hut_theta=float(meta.get('barnes_hut_theta', DEFAULT_THETA)),
            softening_sq=float(meta.get('softening_sq', SOFTENING_SQ)),
            integration_method=meta.get('integration_method', 'symplectic_euler'),
            adaptive_enabled=meta_flag(meta, 'adaptive_enabled'),
            adaptive_eta=float(meta.get('adaptive_eta', 0.02)),
            adaptive_dt_min=float(meta.get('adaptive_dt_min', 1e-10)),
            # creation stores dt_max <= 0 as the job's time_step
            adaptive_dt_max=float(meta.get('adaptive_dt_max', meta.get('time_step', 0.1))),
            gpu_enabled=meta_flag(meta, 'gpu_enabled'),
        )

    @classmethod
    def from_task(cls, task: Any) -> 'EpochParams':
        """The settings a claimed task (workers.base.message_backend.TaskMessage) carries."""
        return cls(
            package_size=task.package_size,
            total_packages=task.total_packages,
            num_epochs=task.num_epochs,
            snapshot_interval=task.snapshot_interval,
            force_algorithm=task.algorithm,
            barnes_hut_theta=task.barnes_hut_theta,
            softening_sq=task.softening_sq,
            integration_method=task.integration_method,
            adaptive_enabled=task.adaptive_enabled,
            adaptive_eta=task.adaptive_eta,
            adaptive_dt_min=task.adaptive_dt_min,
            adaptive_dt_max=task.adaptive_dt_max,
            gpu_enabled=task.gpu_enabled,
        )


def build_epoch_tasks(
    job_id: int,
    generation: int,
    epoch: int,
    n_bodies: int,
    time_step: float,
    params: EpochParams,
) -> List[Dict[str, str]]:
    """Return the stream payloads of all packages of one epoch."""
    p = params
    tasks = []
    for pkg_num in range(1, p.total_packages + 1):
        start_idx = (pkg_num - 1) * p.package_size
        end_idx = min(pkg_num * p.package_size, n_bodies)
        tasks.append(TaskV2(
            job_id=str(job_id),
            epoch=str(epoch),
            package_num=str(pkg_num),
            start_idx=str(start_idx),
            end_idx=str(end_idx),
            total_packages=str(p.total_packages),
            package_size=str(p.package_size),
            gpu_enabled=str(bool(p.gpu_enabled)).lower(),
            time_step=str(time_step),
            snapshot_interval=str(p.snapshot_interval),
            num_epochs=str(p.num_epochs),
            force_algorithm=p.force_algorithm,
            barnes_hut_theta=str(p.barnes_hut_theta),
            softening_sq=str(p.softening_sq),
            integration_method=p.integration_method,
            adaptive_enabled=str(bool(p.adaptive_enabled)).lower(),
            adaptive_eta=str(p.adaptive_eta),
            adaptive_dt_min=str(p.adaptive_dt_min),
            adaptive_dt_max=str(p.adaptive_dt_max),
            generation=str(generation),
        ).to_stream_dict())
    return tasks


# The fields of a task that differ between the packages or epochs of a job.
# Everything else is the same in every task of a job (generation included
# between recoveries, but it is sent per epoch anyway).
PER_TASK_FIELDS = ('job_id', 'epoch', 'package_num', 'start_idx', 'end_idx',
                   'time_step', 'generation')


def epoch_task_template(job_id: int, generation: int, epoch: int, n_bodies: int,
                        time_step: float, params: EpochParams) -> List[str]:
    """The job-level fields every task of an epoch shares, as a flat
    [field, value, ...] list in task order (the publication script builds
    each package's entry from it and the per-task fields, so a merger sends
    them once instead of once per package). Taken from the first task
    build_epoch_tasks would build, so the entries are identical."""
    one = dict(build_epoch_tasks(job_id, generation, epoch, n_bodies, time_step,
                                 _one_package(params))[0])
    one['total_packages'] = str(params.total_packages)
    flat: List[str] = []
    for k, v in one.items():
        if k not in PER_TASK_FIELDS:
            flat.extend([k, v])
    return flat


def _one_package(params: EpochParams) -> EpochParams:
    return dataclasses.replace(params, total_packages=1)


def meta_flag(meta: Dict[str, str], key: str) -> bool:
    """Parse a boolean stored as a string in the Redis job meta hash."""
    return str(meta.get(key, 'False')).lower() in ('true', '1', 'yes')
