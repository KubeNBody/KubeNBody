#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Record the static dashboard demo's fixtures from a live API.

Reads every endpoint the
dashboard uses for the given jobs and saved comparisons, and writes them in the
layout the static demo serves (code/web-dashboard/demo-data/, consumed by
src/lib/demo/query.ts `fixturePaths`):

    manifest.json                      ids, recording time, source git sha, chunk size
    system/{health,metrics,ready,v2-health,v2-stats,cache-stats}.json
    jobs/index.json                    the jobs as /api/jobs lists them
    jobs/{id}/{job,progress,status,packages,v2,metrics,summary}.json
    jobs/{id}/anim/meta.json + {k}.json   animation snapshots, thinned + chunked
    jobs/{id}/results.csv.gz           capped excerpt of the results download
    selectable.json
    compare/{ref}.json                 POST /comparison/compare: ref vs every other demo job
    comparisons/index.json, comparisons/{id}.json
    files/index.json, files/{id}.json, files/{id}.csv

Usage:
    NBODY_USER=user NBODY_PASS=... python -m scripts.export_demo_fixtures \\
        --api http://localhost:8000 --ids demo_job_ids.json \\
        --out code/web-dashboard/demo-data
    (or --jobs 1,2,3 --comparisons 4,5 instead of --ids)
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import requests


# Fields that can carry cluster internals (hostnames, pod names, bucket keys)
SCRUB_KEYS = {
    "lease_owner": "demo-worker",
    "worker_id": "demo-worker",
    "consumer": "demo-worker",
    "hostname": "demo-host",
    "pod_name": "demo-pod",
    "input_file_key": "demo/input.csv",
    "results_file_key": "demo/results.csv",
    "file_key": "demo/input.csv",
    "s3_key": "demo/object",
    "results_key": "demo/object",
    "created_by": "demo",
    "username": "demo",
    "owner": "demo",
}
# max_length of JobToJobComparisonRequest.comparison_job_ids
COMPARE_BATCH = 5
SUSPICIOUS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|svc|\.local\b|azurecr|minio|amazonaws|redis-|rabbitmq|nbody-db", re.I)


def scrub(value, warnings: list[str], path: str = ""):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in SCRUB_KEYS and isinstance(item, str) and item:
                out[key] = SCRUB_KEYS[key]
            else:
                out[key] = scrub(item, warnings, f"{path}.{key}")
        return out
    if isinstance(value, list):
        return [scrub(item, warnings, f"{path}[]") for item in value]
    if isinstance(value, str) and SUSPICIOUS.search(value):
        warnings.append(f"{path}: {value[:120]}")
    return value


def round_sig(x: float, digits: int) -> float:
    if x == 0 or not math.isfinite(x):
        return x
    return round(x, digits - 1 - int(math.floor(math.log10(abs(x)))))


class Api:
    def __init__(self, base: str, user: str, password: str):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        r = self.s.post(f"{self.base}/api/auth/login", json={"username": user, "password": password}, timeout=30)
        r.raise_for_status()
        self.s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def get(self, path: str, **params):
        r = self.s.get(f"{self.base}{path}", params=params or None, timeout=600)
        if not r.ok:
            raise RuntimeError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def get_bytes(self, path: str) -> bytes:
        r = self.s.get(f"{self.base}{path}", timeout=600)
        if not r.ok:
            raise RuntimeError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.content

    def post(self, path: str, body: dict):
        r = self.s.post(f"{self.base}{path}", json=body, timeout=600)
        if not r.ok:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()


class Writer:
    def __init__(self, root: str):
        self.root = root
        self.warnings: list[str] = []

    def json(self, rel: str, data, scrubbed: bool = True):
        if scrubbed:
            data = scrub(data, self.warnings, rel)
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, separators=(",", ":"))

    def bytes(self, rel: str, data: bytes):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)


def thin_indices(total: int, keep: int) -> list[int]:
    """Evenly spaced snapshot indices, always including the first and the last."""
    if total <= keep:
        return list(range(total))
    stride = (total - 1) / (keep - 1)
    return sorted({round(i * stride) for i in range(keep)})


def record_animation(api: Api, w: Writer, job: dict, args) -> set[int]:
    job_id = job["job_id"]
    snapshots, offset, meta = [], 0, None
    while True:
        page = api.get(f"/api/jobs/{job_id}/animation-data/paginated", offset=offset, limit=100)
        meta = meta or page
        snapshots.extend(page["snapshots"])
        offset += len(page["snapshots"])
        if not page["has_next"] or not page["snapshots"]:
            break

    kept = [snapshots[i] for i in thin_indices(len(snapshots), args.max_snapshots)]
    for snap in kept:
        snap["particles"] = [[round_sig(v, args.precision) for v in p] for p in snap["particles"]]
        snap["center_of_mass"] = [round_sig(v, args.precision) for v in snap["center_of_mass"]]

    stride = max(1, round((len(snapshots) - 1) / max(1, len(kept) - 1))) if len(kept) > 1 else 1
    w.json(f"jobs/{job_id}/anim/meta.json", {
        "job_id": job_id,
        "snapshot_interval": meta["snapshot_interval"] * stride,
        "total_epochs": meta["total_epochs"],
        "total_snapshots": len(kept),
        "chunk": args.chunk,
    })
    for k in range(math.ceil(len(kept) / args.chunk)):
        w.json(f"jobs/{job_id}/anim/{k}.json", kept[k * args.chunk:(k + 1) * args.chunk], scrubbed=False)
    print(f"    animation: {len(snapshots)} snapshots -> {len(kept)} kept, "
          f"{math.ceil(len(kept) / args.chunk)} chunks", flush=True)
    return {s["epoch"] for s in kept}


def record_results(api: Api, w: Writer, job: dict, epochs: set[int], args):
    job_id = job["job_id"]
    path = f"/api/v2/jobs/{job_id}/results" if job["job_type"] == "distributed_v2" else f"/api/jobs/{job_id}/results"
    raw = api.get_bytes(path)
    text = gzip.decompress(raw).decode() if raw[:2] == b"\x1f\x8b" else raw.decode()

    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    epoch_col = header.index("epoch") if "epoch" in header else None
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    limit = args.max_results_mb * 1024 * 1024
    truncated = False
    for row in reader:
        # The final state is always kept: v2 results files hold only that epoch
        if epoch_col is not None and row and int(float(row[epoch_col])) not in epochs | {job["num_epochs"]}:
            continue
        if out.tell() > limit:
            truncated = True
            break
        writer.writerow(row)
    note = "# N-body dashboard demo: excerpt of the results"
    note += " at the animation's snapshot epochs" if epoch_col is not None else ""
    note += ", truncated" if truncated else ""
    out.write(note + "\n")
    data = gzip.compress(out.getvalue().encode(), mtime=0)
    w.bytes(f"jobs/{job_id}/results.csv.gz", data)
    print(f"    results: {len(text) / 1e6:.1f} MB -> {out.tell() / 1e6:.2f} MB excerpt ({len(data) / 1e6:.2f} MB gz)",
          flush=True)


def git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=os.environ.get("NBODY_API_URL") or None,
                        required=not os.environ.get("NBODY_API_URL"), help="API base URL (default: $NBODY_API_URL)")
    parser.add_argument("--ids", help="JSON written by scripts/run_demo_jobs.py")
    parser.add_argument("--jobs", help="comma-separated job ids (instead of --ids)")
    parser.add_argument("--comparisons", default="", help="comma-separated saved comparison ids")
    parser.add_argument("--out", required=True)
    parser.add_argument("--chunk", type=int, default=30, help="must equal AnimationViewer's snapshotsPerPage")
    parser.add_argument("--max-snapshots", type=int, default=3000,
                        help="thin longer animations to this many evenly spaced frames")
    # The charts look the same at a few hundred points. Without the cap, the
    # snapshot-dense three-body runs add ~2 MB each to metrics and compare files
    parser.add_argument("--max-chart-snapshots", type=int, default=300,
                        help="max_snapshots of the recorded metrics and job-to-job comparisons")
    parser.add_argument("--precision", type=int, default=5, help="significant digits of particle data")
    parser.add_argument("--max-results-mb", type=float, default=2.0)
    parser.add_argument("--source-sha", help="git sha to record (default: this checkout's HEAD)")
    args = parser.parse_args()

    if args.ids:
        with open(args.ids) as f:
            ids = json.load(f)
        job_ids = sorted(ids["jobs"].values())
        comparison_ids = sorted(ids["comparisons"].values())
        known_file_ids = {int(v) for v in ids.get("files", {}).values() if v is not None}
    elif args.jobs:
        job_ids = sorted(int(x) for x in args.jobs.split(","))
        comparison_ids = sorted(int(x) for x in args.comparisons.split(",") if x)
        known_file_ids = set()
    else:
        sys.exit("pass --ids or --jobs")

    api = Api(args.api, os.environ["NBODY_USER"], os.environ["NBODY_PASS"])
    if os.path.exists(args.out):
        shutil.rmtree(args.out)
    w = Writer(args.out)

    print("system endpoints", flush=True)
    for name, path in [("health", "/api/health"), ("metrics", "/api/metrics"), ("ready", "/api/ready"),
                       ("v2-health", "/api/v2/health"), ("v2-stats", "/api/v2/stats"),
                       ("cache-stats", "/api/comparison/cache/stats")]:
        w.json(f"system/{name}.json", api.get(path))

    listed, page = {}, 1
    while True:
        body = api.get("/api/jobs", page=page, page_size=100)
        listed.update((j["job_id"], j) for j in body["jobs"])
        if page >= body["pages"]:
            break
        page += 1
    missing = [i for i in job_ids if i not in listed]
    if missing:
        sys.exit(f"jobs not in /api/jobs: {missing}")
    w.json("jobs/index.json", [listed[i] for i in job_ids])

    all_files = {f["file_id"]: f for f in api.get("/api/files", page_size=100)["files"]}
    file_ids = set(known_file_ids)
    for job_id in job_ids:
        job = api.get(f"/api/jobs/{job_id}")
        if job["status"] != "COMPLETED":
            sys.exit(f"job {job_id} is {job['status']}, the demo only shows completed jobs")
        print(f"job {job_id} ({job['job_type']})", flush=True)
        if not known_file_ids:
            # Job JSON has no file id. Match the library file by original name (newest wins)
            matches = [f for f in all_files.values() if f["original_filename"] == job.get("filename")]
            if matches:
                file_ids.add(max(matches, key=lambda f: f["created_at"])["file_id"])
        w.json(f"jobs/{job_id}/job.json", job)
        w.json(f"jobs/{job_id}/progress.json", api.get(f"/api/jobs/{job_id}/progress"))
        w.json(f"jobs/{job_id}/status.json", api.get(f"/api/jobs/{job_id}/status"))
        w.json(f"jobs/{job_id}/packages.json", api.get(f"/api/jobs/{job_id}/packages"))
        if job["job_type"] == "distributed_v2":
            w.json(f"jobs/{job_id}/v2.json", api.get(f"/api/v2/jobs/{job_id}"))
        w.json(f"jobs/{job_id}/metrics.json",
               api.get(f"/api/comparison/jobs/{job_id}/metrics", max_snapshots=args.max_chart_snapshots))
        w.json(f"jobs/{job_id}/summary.json", api.get(f"/api/comparison/jobs/{job_id}/summary"))
        epochs = record_animation(api, w, job, args)
        record_results(api, w, job, epochs, args)

    w.json("selectable.json", [j for j in api.get("/api/jobs/selectable") if j["job_id"] in job_ids])

    print("comparisons", flush=True)
    for ref in job_ids:
        # The API compares at most COMPARE_BATCH jobs per request. Merge the batches
        # (jobs on another input come back in skipped_jobs, as the live API reports them)
        others = [i for i in job_ids if i != ref]
        merged = None
        for k in range(0, len(others), COMPARE_BATCH):
            part = api.post("/api/comparison/compare",
                            {"reference_job_id": ref, "comparison_job_ids": others[k:k + COMPARE_BATCH],
                             "max_snapshots": args.max_chart_snapshots})
            if merged is None:
                merged = part
            else:
                merged["comparisons"] += part["comparisons"]
                merged["skipped_jobs"] += part["skipped_jobs"]
                merged["computation_time_ms"] += part["computation_time_ms"]
        w.json(f"compare/{ref}.json", merged)
    saved = {c["comparison_id"]: c for c in api.get("/api/comparison/saved", page_size=100)["comparisons"]}
    w.json("comparisons/index.json", {"comparisons": [saved[i] for i in comparison_ids if i in saved]})
    for cid in comparison_ids:
        w.json(f"comparisons/{cid}.json", api.get(f"/api/comparison/saved/{cid}"))

    print("files", flush=True)
    w.json("files/index.json", [all_files[i] for i in sorted(file_ids) if i in all_files])
    for fid in sorted(file_ids):
        w.json(f"files/{fid}.json", api.get(f"/api/files/{fid}"))
        w.bytes(f"files/{fid}.csv", api.get_bytes(f"/api/files/{fid}/download"))

    w.json("manifest.json", {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_git_sha": args.source_sha or git_sha(),
        "chunk": args.chunk,
        "job_ids": job_ids,
        "comparison_ids": comparison_ids,
        "file_ids": sorted(file_ids),
    }, scrubbed=False)

    total = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(args.out) for f in fs)
    largest = max(((os.path.getsize(os.path.join(d, f)), os.path.join(d, f)) for d, _, fs in os.walk(args.out) for f in fs))
    print(f"\nwrote {args.out}: {total / 1e6:.1f} MB, largest file {largest[1]} ({largest[0] / 1e6:.2f} MB)")
    if w.warnings:
        print(f"\n! {len(w.warnings)} string(s) look like infrastructure details - review before publishing:")
        for warning in sorted(set(w.warnings))[:50]:
            print(f"  {warning}")


if __name__ == "__main__":
    main()
