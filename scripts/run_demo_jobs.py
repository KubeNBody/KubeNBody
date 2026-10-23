#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Run the static dashboard demo's showcase jobs and saved comparisons.

Uploads the inputs written by
scripts/make_demo_inputs.py, submits the jobs below, waits for them, creates the
saved comparisons, and writes the resulting ids for scripts/export_demo_fixtures.py.

Group A (figure-eight, 3 bodies): integrators. Group S: a gallery of
Suvakov-Dmitrasinovic periodic three-body orbits. Group G (two colliding disk
galaxies, 602 bodies): direct vs Barnes-Hut vs distributed v2.

Usage:
    NBODY_USER=user NBODY_PASS=... python -m scripts.run_demo_jobs \\
        --api http://localhost:8000 --inputs code/data/demo --out demo_job_ids.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests


GROUP_A_FILE = ("figure_eight_3body.csv", "Figure-eight (3 bodies)",
                "Three equal stars on a figure eight orbit. One lap takes about 6.3 time units.")
GALAXY_FILE = ("galaxy_collision.csv", "Colliding disk galaxies (602 bodies)",
               "Two disk galaxies, each a heavy core with 300 light stars, set on a close passing orbit.")

GROUP_A = dict(num_epochs=19000, time_step=0.001, snapshot_interval=64, force_algorithm="direct")
# Fixed-step RK4 over two periods, dt per orbit from its closest approach (see make_demo_inputs.py)
GROUP_S = dict(force_algorithm="direct", integration_method="rk4")
# Their snapshot intervals give ~1,450 frames per lap: the close passes are fast,
# and sparser frames draw them as straight chords in the animation trails
GROUP_G = dict(num_epochs=8000, time_step=0.005, snapshot_interval=32, softening_sq=0.0025,
               integration_method="velocity_verlet")

JOBS = [
    ("A1", "mono", GROUP_A_FILE, {**GROUP_A, "integration_method": "velocity_verlet"},
     "Three stars chase each other around a figure eight. Velocity Verlet keeps the loop closed and the energy steady."),
    ("A2", "mono", GROUP_A_FILE, {**GROUP_A, "integration_method": "simple_euler"},
     "Three stars on a figure eight orbit, moved with Simple Euler, the most basic method. The stars slowly drift off the figure eight and the energy keeps growing."),
    ("A3", "mono", GROUP_A_FILE, {**GROUP_A, "integration_method": "rk4"},
     "Three stars on a figure eight orbit, moved with RK4. It is very accurate, but it computes the forces four times per step."),
    ("S1", "mono", ("moth_3body.csv", "Moth I (3 bodies)", "Three equal stars on the moth orbit. One lap takes about 14.9 time units."),
     {**GROUP_S, "time_step": 0.001, "num_epochs": 29788, "snapshot_interval": 10},
     "Moth I. Three stars repeat a dance shaped like a moth, shown for two full laps."),
    ("S2", "mono", ("yin_yang_3body.csv", "Yin-yang Ia (3 bodies)", "Three equal stars on the yin yang orbit. One lap takes about 17.3 time units."),
     {**GROUP_S, "time_step": 0.0001, "num_epochs": 346568, "snapshot_interval": 120},
     "Yin yang Ia. Three stars trace a yin yang pattern, shown for two full laps. Their close passes need very small time steps."),
    ("S3", "mono", ("goggles_3body.csv", "Goggles (3 bodies)", "Three equal stars on the goggles orbit. One lap takes about 10.5 time units."),
     {**GROUP_S, "time_step": 0.0001, "num_epochs": 209336, "snapshot_interval": 72},
     "Goggles. Three stars draw a pair of goggles, shown for two full laps."),
    ("G1", "mono", GALAXY_FILE, {**GROUP_G, "force_algorithm": "direct"},
     "Two disk galaxies swing past each other. Gravity pulls out a bridge of stars and then two long tails. One worker computes the exact forces."),
    ("G2", "mono", GALAXY_FILE, {**GROUP_G, "force_algorithm": "barnes_hut", "barnes_hut_theta": 0.5},
     "Two disk galaxies swing past each other, with the forces from a faster tree method that approximates them. The overall shape is right, but single stars end up slightly off from where exact forces would put them."),
    # gvv_accel: Verlet whose off-package bodies are predicted with x + v*dt + a*dt^2/2,
    # exactly what each package computes for its own bodies, so the distributed run
    # reproduces the monolithic one (plain velocity_verlet predicts linearly and drifts apart)
    ("G3", "v2", GALAXY_FILE, {**GROUP_G, "integration_method": "gvv_accel", "force_algorithm": "direct",
                               "package_size": 151, "partition_strategy": "orb"},
     "Two disk galaxies swing past each other, with the work split across four workers. The result matches one worker doing it all down to the last digits. The collision is chaotic, so those tiny differences grow late in the run."),
]

# Short titles shown in the job list instead of the input file name
NAMES = {
    "A1": "Figure eight with Velocity Verlet", "A2": "Figure eight with Simple Euler", "A3": "Figure eight with RK4",
    "S1": "Moth I orbit", "S2": "Yin yang orbit", "S3": "Goggles orbit",
    "G1": "Galaxy collision with exact forces", "G2": "Galaxy collision with a tree method",
    "G3": "Galaxy collision on four workers",
}

COMPARISONS = [
    ("C1", "A1", ["A2", "A3"]),
    ("C2", "G1", ["G2", "G3"]),
]

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


class Api:
    def __init__(self, base: str, user: str, password: str):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        r = self.s.post(f"{self.base}/api/auth/login", json={"username": user, "password": password}, timeout=30)
        r.raise_for_status()
        # The API reads the Bearer header (the dashboard's /api proxy adds it from its cookie)
        self.s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def get(self, path: str):
        r = self.s.get(f"{self.base}{path}", timeout=60)
        r.raise_for_status()
        return r.json()

    def post_form(self, path: str, data: dict, file_path: str | None = None):
        for attempt in range(2):
            files = None
            if file_path:
                files = {"file": (os.path.basename(file_path), open(file_path, "rb"), "text/csv")}
            r = self.s.post(f"{self.base}{path}", data={k: str(v) for k, v in data.items()}, files=files, timeout=120)
            # A long-idle API can hold a dead RabbitMQ connection. It reconnects on the next publish
            if r.status_code >= 500 and "publish" in r.text and attempt == 0:
                print(f"  publish failed ({r.text[:120]}), retrying once", flush=True)
                time.sleep(2)
                continue
            if not r.ok:
                sys.exit(f"POST {path} failed: {r.status_code} {r.text[:500]}")
            return r.json()

    def library_file_id(self, name: str) -> int | None:
        """Id of an already uploaded input file with this name (e.g. from an earlier, failed run)."""
        r = self.s.get(f"{self.base}/api/files/check-name", params={"name": name}, timeout=30)
        r.raise_for_status()
        body = r.json()
        return int(body["file_id"]) if body.get("exists") else None


def submit(api: Api, engine: str, params: dict, name: str, description: str, file_id: int | None,
           upload: tuple | None) -> int:
    data = {**params, "job_name": name, "job_description": description}
    file_path = None
    if file_id is not None:
        data["file_id"] = file_id
    else:
        file_path, name, file_description = upload
        data.update(user_friendly_name=name, description=file_description)

    if engine == "mono":
        data.update(job_type="monolithic", use_gpu="false")
        body = api.post_form("/api/jobs", data, file_path)
    else:
        data["gpu_enabled"] = "false"
        body = api.post_form("/api/v2/jobs", data, file_path)
    return int(body.get("job_id") or body["job"]["job_id"])


def wait_jobs(api: Api, ids: dict[str, int], timeout: float) -> None:
    t0 = time.time()
    pending = dict(ids)
    while pending and time.time() - t0 < timeout:
        for key, job_id in list(pending.items()):
            job = api.get(f"/api/jobs/{job_id}")
            status = str(job["status"]).upper()
            if status in TERMINAL:
                print(f"  {key} job {job_id}: {status} after {time.time() - t0:.0f}s", flush=True)
                if status != "COMPLETED":
                    sys.exit(f"{key} (job {job_id}) ended {status}")
                del pending[key]
        time.sleep(3)
    if pending:
        sys.exit(f"Timed out waiting for {pending}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=os.environ.get("NBODY_API_URL") or None,
                        required=not os.environ.get("NBODY_API_URL"), help="API base URL (default: $NBODY_API_URL)")
    parser.add_argument("--inputs", required=True, help="directory with make_demo_inputs.py output")
    parser.add_argument("--out", required=True, help="where to write the job/comparison ids (JSON)")
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--reuse", default="", help="completed jobs/comparisons to keep, e.g. A1=41,C1=2")
    parser.add_argument("--only", default="", help="run only these jobs, e.g. A1,A2,A3 (the figure-eight example); "
                                                     "a comparison runs when all its jobs do")
    args = parser.parse_args()
    only = {k for k in args.only.split(",") if k}
    unknown = only - {key for key, *_ in JOBS}
    if unknown:
        sys.exit(f"--only: unknown jobs {sorted(unknown)}")
    reuse = {k: int(v) for k, v in (item.split("=") for item in args.reuse.split(",") if item)}

    api = Api(args.api, os.environ["NBODY_USER"], os.environ["NBODY_PASS"])

    job_ids: dict[str, int] = {}
    file_ids: dict[str, int] = {}
    for key, engine, (fname, name, fdesc), params, description in JOBS:
        if only and key not in only:
            continue
        if fname not in file_ids and (existing := api.library_file_id(name)) is not None:
            file_ids[fname] = existing
            print(f"reusing library file {existing} for {fname}", flush=True)
        if key in reuse:
            job_ids[key] = reuse[key]
            print(f"reusing {key}: job {reuse[key]}", flush=True)
            continue
        upload = None if fname in file_ids else (os.path.join(args.inputs, fname), name, fdesc)
        job_id = submit(api, engine, params, NAMES[key], description, file_ids.get(fname), upload)
        job_ids[key] = job_id
        if fname not in file_ids:
            # The job JSON doesn't carry the library file id. Look the upload up by name
            file_ids[fname] = api.library_file_id(name)
        print(f"submitted {key}: job {job_id} ({engine})", flush=True)

    print("waiting for jobs...", flush=True)
    wait_jobs(api, job_ids, args.timeout)

    comparison_ids: dict[str, int] = {}
    for key, ref, others in COMPARISONS:
        if any(k not in job_ids for k in (ref, *others)):
            continue
        if key in reuse:
            comparison_ids[key] = reuse[key]
            print(f"reusing comparison {key}: {reuse[key]}", flush=True)
            continue
        r = api.s.post(f"{api.base}/api/comparison/saved", timeout=60, json={
            "reference_job_id": job_ids[ref], "comparison_job_ids": [job_ids[o] for o in others],
            # above every demo job's snapshot count, so every snapshot is compared
            "max_snapshots": 1000,
        })
        r.raise_for_status()
        comparison_ids[key] = int(r.json()["comparison_id"])
        print(f"created comparison {key}: {comparison_ids[key]}", flush=True)

    t0 = time.time()
    for key, cid in comparison_ids.items():
        while True:
            status = api.get(f"/api/comparison/saved/{cid}")["status"]
            if status in ("COMPLETED", "COMPLETED_EMPTY", "FAILED"):
                print(f"  {key} comparison {cid}: {status}", flush=True)
                if status != "COMPLETED":
                    sys.exit(f"comparison {key} ended {status}")
                break
            if time.time() - t0 > args.timeout:
                sys.exit(f"Timed out waiting for comparison {key}")
            time.sleep(3)

    result = {"jobs": job_ids, "comparisons": comparison_ids, "files": file_ids}
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
