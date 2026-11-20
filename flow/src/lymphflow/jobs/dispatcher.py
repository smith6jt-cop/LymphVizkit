"""Runs queued jobs one at a time as subprocesses and integrates their results.

Concurrency is 1 by design: the target VM has 2 vCPUs, and the interactive path must keep
one of them. The service only *launches and watches* jobs; all heavy work happens in the
job process so a long computation cannot block request handling.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..db import utcnow
from . import spec as spec_mod


class Dispatcher:
    def __init__(self, state, poll_seconds: float = 0.5):
        self.state = state
        self.poll = poll_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self._reconcile()
        self._thread = threading.Thread(target=self._loop, name="lymphflow-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _reconcile(self) -> None:
        """Jobs left 'running' by a previous service process are not running any more."""
        for row in self.state.db.all("SELECT id FROM jobs WHERE state='running'"):
            jobdir = self.state.settings.jobs_dir / row["id"]
            status = spec_mod.read_status(jobdir)
            if status.get("state") == "done" and (jobdir / "result.json").exists():
                self._finish(row["id"], jobdir, "done")
            else:
                spec_mod.write_status(jobdir, "failed", 0.0, "service restarted while the job was running")
                self._set_state(row["id"], "failed")

    # ---- API -----------------------------------------------------------------
    def submit(self, exp_id: int, owner: str, job_type: str, params: dict) -> str:
        snapshot = self.state.snapshot(exp_id)
        job_id, _ = spec_mod.new_job_dir(self.state.settings, job_type, params, snapshot)
        self.state.db.execute(
            "INSERT INTO jobs(id, experiment_id, type, state, owner_id, created_at) VALUES (?,?,?,?,?,?)",
            (job_id, exp_id, job_type, "queued", owner, utcnow()))
        self._wake.set()
        return job_id

    def cancel(self, job_id: str) -> None:
        jobdir = self.state.settings.jobs_dir / job_id
        (jobdir / "cancel").touch()
        row = self.state.db.one("SELECT state FROM jobs WHERE id=?", (job_id,))
        if row is not None and row["state"] == "queued":
            spec_mod.write_status(jobdir, "cancelled", 0.0, "cancelled before start")
            self._set_state(job_id, "cancelled")
        self._wake.set()

    def describe(self, row) -> dict:
        jobdir = self.state.settings.jobs_dir / row["id"]
        status = spec_mod.read_status(jobdir)
        out = {"id": row["id"], "experiment_id": row["experiment_id"], "type": row["type"],
               "state": row["state"], "created_at": row["created_at"], "finished_at": row["finished_at"],
               "progress": status.get("progress", 0.0), "message": status.get("message", "")}
        if row["state"] == "done":
            out["progress"] = 1.0
            out["result"] = spec_mod.read_json(jobdir / "result.json")
        if row["state"] == "failed":
            log = jobdir / "log.txt"
            out["log_tail"] = log.read_text(errors="replace")[-2000:] if log.exists() else ""
        return out

    # ---- loop ----------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            row = self.state.db.one("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at, id LIMIT 1")
            if row is None:
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue
            try:
                self._run(row)
            except Exception as exc:  # never let one job kill the dispatcher
                jobdir = self.state.settings.jobs_dir / row["id"]
                spec_mod.write_status(jobdir, "failed", 0.0, f"dispatcher error: {exc}")
                self._set_state(row["id"], "failed")

    def _run(self, row) -> None:
        job_id = row["id"]
        jobdir: Path = self.state.settings.jobs_dir / job_id
        if (jobdir / "cancel").exists():
            self._set_state(job_id, "cancelled")
            return
        self._set_state(job_id, "running")
        python = self.state.settings.job_python or sys.executable
        env = dict(os.environ)
        # One job gets one core's worth of threads; the service keeps the other.
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
            env.setdefault(var, "1")
        cache_root = self.state.settings.data_dir / "cache"
        env.setdefault("NUMBA_CACHE_DIR", str(cache_root / "numba"))
        env.setdefault("MPLCONFIGDIR", str(cache_root / "mpl"))
        with open(jobdir / "log.txt", "ab") as log:
            proc = subprocess.Popen([python, "-m", "lymphflow.jobs.run", str(jobdir)],
                                    stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            cancel_seen_at = None
            while proc.poll() is None:
                if self._stop.is_set():
                    proc.terminate()
                    break
                if (jobdir / "cancel").exists():
                    cancel_seen_at = cancel_seen_at or time.time()
                    if time.time() - cancel_seen_at > 10:   # did not stop at a checkpoint
                        proc.terminate()
                time.sleep(self.poll)
            code = proc.wait()
        status = spec_mod.read_status(jobdir)
        state = status.get("state")
        if (jobdir / "cancel").exists() and state != "done":
            spec_mod.write_status(jobdir, "cancelled", 0.0, "cancelled")
            self._set_state(job_id, "cancelled")
        elif code == 0 and state == "done":
            self._finish(job_id, jobdir, "done")
        else:
            if state != "failed":
                spec_mod.write_status(jobdir, "failed", 0.0, f"job process exited with code {code}")
            self._set_state(job_id, "failed")

    # ---- integration ---------------------------------------------------------
    def _finish(self, job_id: str, jobdir: Path, state: str) -> None:
        row = self.state.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is not None and row["type"] == "apply_template" and not row["integrated"]:
            self._integrate_stats(jobdir)
        self.state.db.execute("UPDATE jobs SET state=?, finished_at=?, integrated=1 WHERE id=?",
                              (state, utcnow(), job_id))

    def _integrate_stats(self, jobdir: Path) -> None:
        """Only the service writes SQLite: copy the job's statistics into population_stats."""
        rows = spec_mod.read_json(jobdir / "out" / "stats.json", [])
        live = {r["id"] for r in self.state.db.all("SELECT id FROM samples")}
        with self.state.db.transaction() as c:
            for r in rows:
                if r["sample_id"] not in live:
                    continue
                c.execute(
                    "INSERT INTO population_stats(sample_id, gate_id, gate_key, stats, computed_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(sample_id, gate_id) DO UPDATE SET "
                    "gate_key=excluded.gate_key, stats=excluded.stats, computed_at=excluded.computed_at",
                    (r["sample_id"], r["gate_id"], r["key"], json.dumps(r["stats"]), utcnow()))

    def _set_state(self, job_id: str, state: str) -> None:
        finished = utcnow() if state in ("failed", "cancelled", "done") else None
        self.state.db.execute("UPDATE jobs SET state=?, finished_at=COALESCE(?, finished_at) WHERE id=?",
                              (state, finished, job_id))
