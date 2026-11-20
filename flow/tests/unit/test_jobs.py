"""Job runner: FlowSOM (optional 'jobs' extra), cancellation, the sbatch-compatible entry point."""
import json
import subprocess
import sys

import pytest

from conftest import wait_job
from lymphflow.jobs import spec as spec_mod
from test_api import workspace  # noqa: F401  (fixture)

pytest.importorskip("flowsom", reason="optional 'jobs' extra not installed")
pytestmark = pytest.mark.jobs

FLUORO = ["B530-A", "YG585-A", "R670-A", "V450-A"]


def run_flowsom(client, w, **extra):
    body = {"type": "flowsom", "gate_id": w["gates"]["cells"], "channels": FLUORO, "n_per_sample": 3000,
            "xdim": 6, "ydim": 6, "n_clusters": 6, "seed": 7, **extra}
    res = client.post(f"/api/experiments/{w['exp']}/jobs", json=body)
    assert res.status_code == 200, res.text
    job = wait_job(client, res.json())
    assert job["state"] == "done", job
    return job["result"]


def test_flowsom_recovers_the_four_synthetic_populations(client, workspace):  # noqa: F811
    res = run_flowsom(client, workspace)
    assert res["n_train"] == 4 * 3000 and res["params"]["seed"] == 7 and not res["skipped"]
    assert len(res["clusters"]) == len(res["medians"]) and all(len(r) == 4 for r in res["medians"])
    for s in res["samples"]:
        t = workspace["truth"][s["name"] + ".fcs"]
        f = res["frequencies"][str(s["id"])]
        assert f["n_mapped"] == t["n_cells"] == s["n_population"]        # ALL gated events mapped, not the subsample
        assert sum(f["counts"]) == t["n_cells"] and sum(f["pct"]) == pytest.approx(100.0)
    # every true population is one metacluster pattern: (CD3, CD4, CD8, CD20) high/low
    patterns = {tuple(v > 0.5 for v in row) for row in res["medians"] if row[0] is not None}
    assert {(True, True, False, False), (True, False, True, False), (False, False, False, True),
            (False, False, False, False)} <= patterns
    # frequencies of the B-cell pattern equal the ground truth exactly (clusters are separable)
    b_idx = [i for i, row in enumerate(res["medians"]) if row[0] is not None and tuple(v > 0.5 for v in row) == (False, False, False, True)]
    for s in res["samples"]:
        t = workspace["truth"][s["name"] + ".fcs"]
        assert sum(res["frequencies"][str(s["id"])]["counts"][i] for i in b_idx) == t["counts"]["B cell"]


def test_flowsom_is_reproducible_for_a_fixed_seed(client, workspace):  # noqa: F811
    a, b = run_flowsom(client, workspace), run_flowsom(client, workspace)
    assert a["medians"] == b["medians"] and a["frequencies"] == b["frequencies"]


def test_flowsom_request_validation(client, workspace):  # noqa: F811
    url = f"/api/experiments/{workspace['exp']}/jobs"
    assert client.post(url, json={"type": "flowsom", "channels": ["B530-A"]}).status_code == 422
    assert client.post(url, json={"type": "flowsom", "channels": ["B530-A", "NOPE"]}).status_code == 404
    assert client.post(url, json={"type": "flowsom", "channels": FLUORO, "xdim": 99}).status_code == 422
    assert client.post(url, json={"type": "nope"}).status_code == 422


def test_job_entry_point_runs_from_a_job_directory_alone(client, workspace, tmp_path):  # noqa: F811
    """The exact command an sbatch script would run: no service, no database access."""
    state = client.app.state.lymphflow
    job_id, jobdir = spec_mod.new_job_dir(state.settings, "apply_template", {}, state.snapshot(workspace["exp"]))
    done = subprocess.run([sys.executable, "-m", "lymphflow.jobs.run", str(jobdir)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert spec_mod.read_status(jobdir)["state"] == "done"
    result = json.loads((jobdir / "result.json").read_text())
    assert result["n_failed"] == 0 and result["n_evaluated"] == 4 * 5
    rows = json.loads((jobdir / "out" / "stats.json").read_text())
    s = workspace["samples"][0]
    row = next(r for r in rows if r["sample_id"] == s["id"] and r["gate_id"] == workspace["gates"]["cd4"])
    assert row["stats"]["count"] == workspace["truth"][s["name"] + ".fcs"]["counts"]["CD4 T cell"]
    script = spec_mod.sbatch_script(jobdir)
    assert f"-m lymphflow.jobs.run {jobdir}" in script and script.startswith("#!/bin/bash")


def test_cancelled_job_stops_and_unknown_job_type_fails_visibly(client, workspace, tmp_path):  # noqa: F811
    state = client.app.state.lymphflow
    _, jobdir = spec_mod.new_job_dir(state.settings, "apply_template", {}, state.snapshot(workspace["exp"]))
    (jobdir / "cancel").touch()
    assert subprocess.run([sys.executable, "-m", "lymphflow.jobs.run", str(jobdir)]).returncode == 0
    assert spec_mod.read_status(jobdir)["state"] == "cancelled" and not (jobdir / "result.json").exists()
    _, bad = spec_mod.new_job_dir(state.settings, "does_not_exist", {}, state.snapshot(workspace["exp"]))
    assert subprocess.run([sys.executable, "-m", "lymphflow.jobs.run", str(bad)], capture_output=True).returncode == 1
    assert spec_mod.read_status(bad)["state"] == "failed" and "unknown job type" in spec_mod.read_status(bad)["message"]
