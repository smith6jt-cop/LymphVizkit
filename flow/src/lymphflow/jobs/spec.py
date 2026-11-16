"""Job directories.

A job is a directory, not a database row:

    <jobs_dir>/<id>/spec.json     immutable input: type, params, experiment snapshot, data paths
                    status.json   state / progress / heartbeat (atomically replaced)
                    log.txt       stdout + stderr of the job process
                    result.json   written on success
                    out/          array outputs
                    cancel        sentinel: the job stops at its next checkpoint

A job process reads spec.json and the Parquet event store and writes only inside its own
directory and the content-addressed mask cache. It never opens SQLite. The same entry point,
`python -m lymphflow.jobs.run <jobdir>`, serves a local subprocess and an sbatch script
(see `sbatch_script`).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from ..config import ENGINE_VERSION, Settings

STATES = ("queued", "running", "done", "failed", "cancelled")


def new_job_dir(settings: Settings, job_type: str, params: dict, snapshot: dict) -> tuple[str, Path]:
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    jobdir = settings.jobs_dir / job_id
    (jobdir / "out").mkdir(parents=True)
    spec = {
        "id": job_id,
        "type": job_type,
        "params": params,
        "engine_version": ENGINE_VERSION,
        "events_dir": str(settings.events_dir),
        "masks_dir": str(settings.masks_dir),
        "snapshot": snapshot,
    }
    write_json(jobdir / "spec.json", spec)
    write_status(jobdir, "queued", 0.0, "queued")
    return job_id, jobdir


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_status(jobdir: Path, state: str, progress: float, message: str = "", **extra) -> None:
    write_json(Path(jobdir) / "status.json",
               {"state": state, "progress": round(float(progress), 4), "message": message,
                "heartbeat": time.time(), "pid": os.getpid(), **extra})


def read_status(jobdir: Path) -> dict:
    return read_json(Path(jobdir) / "status.json", {"state": "queued", "progress": 0.0, "message": ""})


def sbatch_script(jobdir: Path, python: str = "python", cpus: int = 4, mem_gb: int = 16,
                  time_limit: str = "02:00:00") -> str:
    """An sbatch script that runs this job. Not submitted by the service; see flow/README.md."""
    return "\n".join([
        "#!/bin/bash",
        f"#SBATCH --job-name=lymphflow-{Path(jobdir).name}",
        f"#SBATCH --cpus-per-task={cpus}",
        f"#SBATCH --mem={mem_gb}gb",
        f"#SBATCH --time={time_limit}",
        f"#SBATCH --output={jobdir}/log.txt",
        "export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK NUMBA_NUM_THREADS=$SLURM_CPUS_PER_TASK",
        f"{python} -m lymphflow.jobs.run {jobdir}",
        "",
    ])
