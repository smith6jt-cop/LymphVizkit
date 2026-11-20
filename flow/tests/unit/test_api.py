"""The HTTP API end to end on synthetic data (in-process TestClient, real job subprocesses)."""
import csv
import io

import numpy as np
import pytest

from conftest import make_client, upload, wait_job
from lymphflow import synthetic
from lymphflow.core import hist

SG = synthetic.SCATTER_GATE


def frame(client, path, **params):
    res = client.get(path, params=params)
    assert res.status_code == 200, res.text
    return hist.unpack_frame(res.content)


@pytest.fixture()
def workspace(client, synth):
    """Experiment with the 4 synthetic files and the template Cells > CD3+ > CD4 T, Cells > B cells."""
    out, manifest = synth
    exp = client.post("/api/experiments", json={"name": "synthetic"}).json()["id"]
    results = upload(client, exp, [out / n for n in sorted(manifest["files"])])
    assert all(r["ok"] for r in results)
    detail = client.get(f"/api/experiments/{exp}").json()
    for s in detail["samples"]:
        client.patch(f"/api/samples/{s['id']}", json={"group_label": manifest["files"][s["name"] + ".fcs"]["group"]})
    scale_id = {c["pnn"]: c["scale_id"] for c in detail["channels"]}
    thr = client.post("/api/scales/apply", json={"scale_id": scale_id["B530-A"],
                                                 "values": [synthetic.GATE_THRESHOLD]}).json()["values"][0]

    def gate(**body):
        res = client.post(f"/api/experiments/{exp}/gates", json=body)
        assert res.status_code == 200, res.text
        return res.json()["id"]

    cells = gate(name="Cells", kind="rect", dims=["FSC-A", "SSC-A"], geom=SG)
    cd3 = gate(name="CD3+", parent_id=cells, kind="range", dims=[{"pnn": "B530-A", "scale_id": scale_id["B530-A"]}],
               geom={"x0": thr, "x1": None})
    cd4 = gate(name="CD4 T", parent_id=cd3, kind="polygon", dims=["YG585-A", "R670-A"],
               geom={"vertices": [[thr, -5], [5, -5], [5, thr], [thr, thr]]})
    b = gate(name="B cells", parent_id=cells, kind="range", dims=["V450-A"], geom={"x0": thr, "x1": None})
    samples = client.get(f"/api/experiments/{exp}").json()["samples"]
    return {"exp": exp, "samples": samples, "truth": manifest["files"], "thr": thr, "scale_id": scale_id,
            "gates": {"cells": cells, "cd3": cd3, "cd4": cd4, "b": b}}


def counts(client, sample_id):
    pops = client.get(f"/api/samples/{sample_id}/populations").json()
    return {p["name"]: p for p in pops["populations"]}


# ---- ingest ------------------------------------------------------------------------------------------
def test_upload_ingests_and_discards_identifying_keywords(client, synth):
    out, manifest = synth
    exp = client.post("/api/experiments", json={"name": "e"}).json()["id"]
    name = sorted(manifest["files"])[0]
    (r,) = upload(client, exp, [out / name])
    assert r["ok"] and r["sample"]["comp_state"] == "embedded" and r["sample"]["keywords_dropped"] >= 11
    sample = client.get(f"/api/samples/{r['sample']['id']}").json()
    blob = str(sample["keywords"]).lower()
    assert "jane" not in blob and "mrn" not in blob and "patient" not in blob and "date" not in sample["keywords"]
    assert sample["keywords"]["cyt"] == "SyntheticCytometer"
    # the uploaded FCS itself is not kept anywhere under the data directory
    data_dir = client.app.state.lymphflow.settings.data_dir
    assert not list(data_dir.rglob("*.fcs"))
    for f in data_dir.rglob("*"):
        if f.is_file():
            assert b"Jane Operator" not in f.read_bytes(), f


def test_non_fcs_upload_is_reported_per_file(client, tmp_path):
    exp = client.post("/api/experiments", json={"name": "e"}).json()["id"]
    bad = tmp_path / "notes.fcs"
    bad.write_text("this is not an FCS file")
    (r,) = upload(client, exp, [bad])
    assert r["ok"] is False and "FCS" in r["error"]
    assert client.get(f"/api/experiments/{exp}").json()["samples"] == []


def test_upload_size_limit(tmp_path, synth):
    out, manifest = synth
    with make_client(tmp_path, LYMPH_FLOW_MAX_UPLOAD_MB=0) as c:
        exp = c.post("/api/experiments", json={"name": "e"}).json()["id"]
        path = out / sorted(manifest["files"])[0]
        with open(path, "rb") as fh:
            res = c.post(f"/api/experiments/{exp}/samples", files=[("files", (path.name, fh, "application/octet-stream"))])
        assert res.status_code == 413
        assert not list((tmp_path / "data" / "incoming").iterdir())      # the partial upload is removed


def test_folder_registration_is_off_unless_configured(client, tmp_path, synth):
    exp = client.post("/api/experiments", json={"name": "e"}).json()["id"]
    assert client.post(f"/api/experiments/{exp}/register-folder", json={"path": str(synth[0])}).status_code == 403


def test_folder_registration_is_confined_to_the_allowed_roots(tmp_path, synth):
    out, manifest = synth
    with make_client(tmp_path, LYMPH_FLOW_REGISTER_ROOTS=str(out)) as c:
        exp = c.post("/api/experiments", json={"name": "e"}).json()["id"]
        assert c.post(f"/api/experiments/{exp}/register-folder", json={"path": str(out.parent)}).status_code == 403
        assert c.post(f"/api/experiments/{exp}/register-folder", json={"path": "/etc"}).status_code == 403
        res = c.post(f"/api/experiments/{exp}/register-folder", json={"path": str(out)}).json()
        assert sum(r["ok"] for r in res["results"]) == len(manifest["files"])


# ---- gating ----------------------------------------------------------------------------------------------
def test_population_counts_match_ground_truth_for_every_sample(client, workspace):
    for s in workspace["samples"]:
        t = workspace["truth"][s["name"] + ".fcs"]
        c = counts(client, s["id"])
        assert c["Cells"]["count"] == t["n_cells"]
        assert c["CD3+"]["count"] == t["counts"]["CD4 T cell"] + t["counts"]["CD8 T cell"]
        assert c["CD4 T"]["count"] == t["counts"]["CD4 T cell"]
        assert c["B cells"]["count"] == t["counts"]["B cell"]
        assert c["CD4 T"]["path"] == "Cells / CD3+ / CD4 T"
        assert c["CD4 T"]["pct_parent"] == pytest.approx(100 * c["CD4 T"]["count"] / c["CD3+"]["count"])


def test_density_frame_totals_and_gate_overlay(client, workspace):
    s = workspace["samples"][0]
    t = workspace["truth"][s["name"] + ".fcs"]
    header, bins = frame(client, f"/api/samples/{s['id']}/plots/density", x="FSC-A", y="SSC-A")
    assert int(bins.sum()) == header["n_parent"] == t["n_events"] and header["nb"] == 256
    (cells,) = header["gates"]
    assert cells["name"] == "Cells" and cells["count"] == t["n_cells"] and not cells["scale_mismatch"]
    # axes swapped relative to the gate's own dimension order -> outline is transposed, count unchanged
    g = workspace["gates"]
    header, bins = frame(client, f"/api/samples/{s['id']}/plots/density", x="R670-A", y="YG585-A", parent=g["cd3"])
    assert int(bins.sum()) == header["n_parent"] == t["counts"]["CD4 T cell"] + t["counts"]["CD8 T cell"]
    (cd4,) = header["gates"]
    assert cd4["count"] == t["counts"]["CD4 T cell"]
    assert header["comp"] == {"state": "embedded", "applied": True}
    assert any(tick["label"] == "10³" for tick in header["x"]["ticks"])


def test_histogram_frame(client, workspace):
    s = workspace["samples"][0]
    t = workspace["truth"][s["name"] + ".fcs"]
    header, bins = frame(client, f"/api/samples/{s['id']}/plots/histogram", x="V450-A", parent=workspace["gates"]["cells"])
    assert int(bins.sum()) == t["n_cells"] and header["y"] is None
    assert [(g["name"], g["count"]) for g in header["gates"]] == [("B cells", t["counts"]["B cell"])]


def test_off_scale_events_are_reported(client, workspace):
    s = workspace["samples"][0]
    header, _ = frame(client, f"/api/samples/{s['id']}/plots/density", x="YG585-A", y="R670-A",
                      parent=workspace["gates"]["cells"])
    assert header["off_scale"]["y"]["below"] > 0          # negatives below the logicle window
    assert header["n_shown"] == header["n_parent"]         # still drawn (on the border), still counted


def test_changing_the_display_scale_does_not_move_a_gate(client, workspace):
    """Design rule 1: gates bind to the scale they were drawn on."""
    s = workspace["samples"][0]
    before = counts(client, s["id"])["CD4 T"]["count"]
    for pnn in ("YG585-A", "R670-A", "B530-A"):
        res = client.put(f"/api/experiments/{workspace['exp']}/scales",
                         json={"pnn": pnn, "kind": "asinh", "params": {"t": 262144, "cofactor": 150}})
        assert res.status_code == 200 and res.json()["scale"]["describe"].startswith("arcsinh cofactor=150")
    assert counts(client, s["id"])["CD4 T"]["count"] == before
    header, _ = frame(client, f"/api/samples/{s['id']}/plots/density", x="YG585-A", y="R670-A",
                      parent=workspace["gates"]["cd3"])
    (cd4,) = header["gates"]
    assert cd4["scale_mismatch"] is True and cd4["count"] == before and len(cd4["outline"]) > 4
    assert header["x"]["scale"]["kind"] == "asinh"
    # a gate drawn now binds to the new scale
    new = client.post(f"/api/experiments/{workspace['exp']}/gates", json={
        "name": "new", "parent_id": workspace["gates"]["cd3"], "kind": "range", "dims": ["YG585-A"],
        "geom": {"x0": 0.5, "x1": None}}).json()["id"]
    dims = {g["id"]: g["dims"] for g in client.get(f"/api/experiments/{workspace['exp']}/gates").json()["gates"]}
    assert dims[new][0]["scale"]["kind"] == "asinh" and dims[workspace["gates"]["cd4"]][0]["scale"]["kind"] == "logicle"


def test_invalid_scale_and_gate_requests_are_rejected(client, workspace):
    exp = workspace["exp"]
    r = client.put(f"/api/experiments/{exp}/scales", json={"pnn": "YG585-A", "kind": "logicle", "params": {"t": 262144, "w": 9, "m": 4.5}})
    assert r.status_code == 422 and r.json()["error"] == "invalid_scale"
    assert client.put(f"/api/experiments/{exp}/scales", json={"pnn": "NOPE", "kind": "linear"}).status_code == 404
    bad = [
        {"name": "x", "kind": "polygon", "dims": ["FSC-A", "SSC-A"], "geom": {"vertices": [[0, 0], [1, 1]]}},
        {"name": "x", "kind": "rect", "dims": ["FSC-A", "FSC-A"], "geom": SG},
        {"name": "x", "kind": "rect", "dims": ["FSC-A"], "geom": SG},
        {"name": "", "kind": "rect", "dims": ["FSC-A", "SSC-A"], "geom": SG},
    ]
    for body in bad:
        assert client.post(f"/api/experiments/{exp}/gates", json=body).status_code == 422, body
    assert client.post(f"/api/experiments/{exp}/gates", json={"name": "x", "kind": "rect", "dims": ["FSC-A", "NOPE"], "geom": SG}).status_code == 404
    dup = client.post(f"/api/experiments/{exp}/gates", json={"name": "Cells", "kind": "rect", "dims": ["FSC-A", "SSC-A"], "geom": SG})
    assert dup.status_code == 409


def test_stale_template_revision_is_a_conflict(client, workspace):
    exp = workspace["exp"]
    rev = client.get(f"/api/experiments/{exp}/gates").json()["revision"]
    body = {"name": "late", "kind": "rect", "dims": ["FSC-A", "SSC-A"], "geom": SG, "expected_revision": rev - 1}
    assert client.post(f"/api/experiments/{exp}/gates", json=body).status_code == 409
    assert client.post(f"/api/experiments/{exp}/gates", json={**body, "expected_revision": rev}).status_code == 200


def test_per_sample_override_changes_only_that_sample_and_is_flagged(client, workspace):
    a, b = workspace["samples"][:2]
    g = workspace["gates"]["b"]
    before_a, before_b = counts(client, a["id"])["B cells"]["count"], counts(client, b["id"])["B cells"]["count"]
    r = client.put(f"/api/gates/{g}/overrides/{a['id']}", json={"geom": {"x0": 5.0, "x1": None}})   # above every event
    assert r.status_code == 200
    pa, pb = counts(client, a["id"])["B cells"], counts(client, b["id"])["B cells"]
    assert pa["count"] == 0 and pa["overridden"] is True
    assert pb["count"] == before_b and pb["overridden"] is False
    rows = list(csv.DictReader(io.StringIO("\n".join(
        l for l in client.get(f"/api/experiments/{workspace['exp']}/stats.csv").text.splitlines() if not l.startswith("#")))))
    flagged = {r_["sample"] for r_ in rows if r_["population"] == "Cells / B cells" and r_["gate_overridden"] == "TRUE"}
    assert flagged == {a["name"]}
    client.delete(f"/api/gates/{g}/overrides/{a['id']}")
    assert counts(client, a["id"])["B cells"]["count"] == before_a


def test_deleting_a_gate_removes_its_descendants(client, workspace):
    client.delete(f"/api/gates/{workspace['gates']['cd3']}")
    assert set(counts(client, workspace["samples"][0]["id"])) == {"Cells", "B cells"}


# ---- compensation state ------------------------------------------------------------------------------------
def test_switching_compensation_off_is_visible_everywhere(client, workspace):
    s = workspace["samples"][0]
    g = workspace["gates"]
    on = client.get(f"/api/samples/{s['id']}/stats", params={"gate": g["cd3"]}).json()
    assert on["values"] == "compensated linear"
    assert client.put(f"/api/samples/{s['id']}/compensation", json={"enabled": False}).json()["applied"] is False
    off = client.get(f"/api/samples/{s['id']}/stats", params={"gate": g["cd3"]}).json()
    assert off["values"].startswith("linear (no compensation") and off["comp_hash"] == "none"
    assert off["channels"]["YG585-A"]["median"] != on["channels"]["YG585-A"]["median"]   # not served from cache
    header, _ = frame(client, f"/api/samples/{s['id']}/plots/density", x="YG585-A", y="R670-A")
    assert header["comp"] == {"state": "embedded", "applied": False}
    matrix = client.get(f"/api/samples/{s['id']}/compensation").json()
    assert matrix["detectors"] == synthetic.FLUORO and np.allclose(matrix["matrix"], synthetic.SPILL)


def test_unusable_matrix_blocks_the_sample_until_resolved(client, tmp_path):
    channels = [*synthetic.CHANNELS]
    channels[5] = "V510-A"
    events, _ = synthetic.make_events({"CD4 T cell": 100, "CD8 T cell": 100, "B cell": 100, "Other": 100}, 20, seed=2)
    path = tmp_path / "bad_matrix.fcs"
    synthetic.write_fcs(path, events, channels=channels)
    exp = client.post("/api/experiments", json={"name": "e"}).json()["id"]
    (r,) = upload(client, exp, [path])
    sid = r["sample"]["id"]
    assert r["sample"]["comp_state"] == "invalid" and "V450-A" in r["sample"]["comp_message"]
    res = client.get(f"/api/samples/{sid}/plots/density", params={"x": "FSC-A", "y": "SSC-A"})
    assert res.status_code == 409 and res.json()["error"] == "compensation_invalid"
    assert client.get(f"/api/samples/{sid}/populations").json()["error"]["kind"] == "compensation_invalid"
    assert client.put(f"/api/samples/{sid}/compensation", json={"enabled": True}).status_code == 409
    assert client.put(f"/api/samples/{sid}/compensation", json={"enabled": False}).status_code == 200
    assert client.get(f"/api/samples/{sid}/plots/density", params={"x": "FSC-A", "y": "SSC-A"}).status_code == 200


# ---- template across samples, mismatched panel --------------------------------------------------------------
@pytest.fixture()
def with_other_panel(client, workspace, tmp_path):
    """Adds a 5th sample whose CD20 detector has a different name (a different panel)."""
    channels = [*synthetic.CHANNELS]
    channels[5] = "V510-A"
    events, _ = synthetic.make_events({"CD4 T cell": 300, "CD8 T cell": 200, "B cell": 400, "Other": 100}, 50, seed=9, spill=None)
    path = tmp_path / "other_panel.fcs"
    synthetic.write_fcs(path, events, spill=None, channels=channels)
    (r,) = upload(client, workspace["exp"], [path])
    return {**workspace, "odd": r["sample"]}


def test_template_job_reports_a_mismatched_panel_instead_of_skipping_it(client, with_other_panel):
    w = with_other_panel
    pops = counts(client, w["odd"]["id"])
    assert pops["Cells"]["count"] == 1000 and pops["CD4 T"]["count"] == 300      # gates it can evaluate still work
    assert pops["B cells"]["count"] is None and "V450-A" in pops["B cells"]["error"]

    job = wait_job(client, client.post(f"/api/experiments/{w['exp']}/jobs", json={"type": "apply_template"}).json())
    assert job["state"] == "done", job
    res = job["result"]
    assert res["n_samples"] == 5 and res["n_populations"] == 5
    assert res["n_failed"] == 1 and res["n_evaluated"] == 5 * 5 - 1
    (failure,) = res["failures"]
    assert failure["sample"] == "other_panel" and failure["population"] == "Cells / B cells" and "V450-A" in failure["error"]

    text = client.get(f"/api/experiments/{w['exp']}/stats.csv").text
    lines = text.splitlines()
    assert lines[0].startswith("# lymphflow statistics export") and "gating template revision" in lines[1]
    rows = list(csv.DictReader(io.StringIO("\n".join(l for l in lines if not l.startswith("#")))))
    err = [r for r in rows if r["error"]]
    assert len(err) == 1 and err[0]["sample"] == "other_panel" and err[0]["population"] == "Cells / B cells"
    for s in w["samples"]:
        t = w["truth"][s["name"] + ".fcs"]
        mine = {r["population"]: r for r in rows if r["sample"] == s["name"] and r["channel"] == "YG585-A"}
        assert int(mine["Cells / CD3+ / CD4 T"]["count"]) == t["counts"]["CD4 T cell"]
        assert float(mine["Cells / CD3+ / CD4 T"]["median"]) == pytest.approx(t["true_fluoro_median"]["CD4 T cell"]["CD4"], rel=1e-5)
        assert mine["Cells / CD3+ / CD4 T"]["values"] == "compensated linear" and mine["All events"]["group"] == t["group"]

    summary = client.get(f"/api/experiments/{w['exp']}/summary", params={"gate": w["gates"]["b"], "metric": "pct_parent"}).json()
    by_sample = {r["sample"]: r for r in summary["rows"]}
    assert by_sample["other_panel"]["value"] is None and "V450-A" in by_sample["other_panel"]["error"]
    assert all(by_sample[s["name"]]["value"] is not None for s in w["samples"])


def test_job_statistics_equal_interactive_statistics(client, workspace):
    s = workspace["samples"][0]
    g = workspace["gates"]["cd4"]
    interactive = client.get(f"/api/samples/{s['id']}/stats", params={"gate": g}).json()
    state = client.app.state.lymphflow
    state.db.execute("DELETE FROM population_stats")
    job = wait_job(client, client.post(f"/api/experiments/{workspace['exp']}/jobs", json={"type": "apply_template"}).json())
    assert job["state"] == "done" and job["result"]["n_failed"] == 0
    assert state.db.one("SELECT COUNT(*) AS n FROM population_stats")["n"] == 4 * 5      # integrated by the service
    from_job = client.get(f"/api/samples/{s['id']}/stats", params={"gate": g}).json()
    assert from_job["channels"] == interactive["channels"] and from_job["count"] == interactive["count"]


def test_summary_metrics(client, workspace):
    exp, g = workspace["exp"], workspace["gates"]["cd4"]
    rows = client.get(f"/api/experiments/{exp}/summary", params={"gate": g, "metric": "count"}).json()["rows"]
    assert {r["sample"]: r["value"] for r in rows} == {
        s["name"]: workspace["truth"][s["name"] + ".fcs"]["counts"]["CD4 T cell"] for s in workspace["samples"]}
    assert {r["group"] for r in rows} == {"Resting", "Reactive"}
    med = client.get(f"/api/experiments/{exp}/summary", params={"gate": g, "metric": "median", "channel": "YG585-A"}).json()["rows"]
    assert all(12000 < r["value"] < 32000 for r in med)
    assert client.get(f"/api/experiments/{exp}/summary", params={"gate": g, "metric": "median"}).status_code == 422
    assert client.get(f"/api/experiments/{exp}/summary", params={"gate": 9999}).status_code == 404


# ---- identity, isolation, serving ------------------------------------------------------------------------------
def test_proxy_mode_requires_the_identity_header_and_isolates_users(tmp_path):
    with make_client(tmp_path, LYMPH_FLOW_AUTH_MODE="proxy", LYMPH_FLOW_USER_HEADER="X-Remote-User") as c:
        assert c.get("/api/me").status_code == 401
        assert c.get("/api/me", headers={"X-Remote-User": "bad user;"}).status_code == 400
        alice, bob = {"X-Remote-User": "alice"}, {"X-Remote-User": "bob"}
        exp = c.post("/api/experiments", json={"name": "alice's"}, headers=alice).json()["id"]
        assert [e["id"] for e in c.get("/api/experiments", headers=alice).json()] == [exp]
        assert c.get("/api/experiments", headers=bob).json() == []
        assert c.get(f"/api/experiments/{exp}", headers=bob).status_code == 404
        assert c.delete(f"/api/experiments/{exp}", headers=bob).status_code == 404
        assert c.post(f"/api/experiments/{exp}/gates", headers=bob,
                      json={"name": "x", "kind": "rect", "dims": ["a", "b"], "geom": SG}).status_code == 404


def test_proxy_mode_refuses_to_start_without_a_header_name():
    from lymphflow.config import load_settings

    with pytest.raises(ValueError, match="LYMPH_FLOW_USER_HEADER"):
        load_settings({"LYMPH_FLOW_AUTH_MODE": "proxy"})
    with pytest.raises(ValueError):
        load_settings({"LYMPH_FLOW_AUTH_MODE": "open"})


def test_dev_mode_refuses_a_non_loopback_bind(monkeypatch, tmp_path, capsys):
    from lymphflow.__main__ import main

    monkeypatch.setenv("LYMPH_FLOW_DATA", str(tmp_path / "d"))
    monkeypatch.setenv("LYMPH_FLOW_HOST", "0.0.0.0")
    monkeypatch.delenv("LYMPH_FLOW_AUTH_MODE", raising=False)
    assert main(["serve"]) == 2
    assert "may only bind to loopback" in capsys.readouterr().err


def test_served_under_a_url_prefix_with_relative_assets(tmp_path):
    with make_client(tmp_path, LYMPH_FLOW_URL_PREFIX="/flow") as c:
        r = c.get("/flow", follow_redirects=False)
        assert r.status_code == 308 and r.headers["location"] == "/flow/"
        page = c.get("/flow/")
        assert page.status_code == 200 and 'src="js/app.js"' in page.text and "http" not in page.text.split("<body>")[1]
        assert c.get("/flow/api/health").json()["status"] == "ok"
        assert c.get("/flow/js/app.js").status_code == 200 and c.get("/flow/vendor/plotly-cartesian.min.js").status_code == 200
        assert c.get("/api/health").status_code == 404
        csp = page.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "script-src 'self'" in csp and "unsafe-eval" not in csp
        assert page.headers["x-content-type-options"] == "nosniff"


def test_vendored_files_match_their_pinned_digests():
    import hashlib
    import json
    from pathlib import Path

    import lymphflow

    vendor = Path(lymphflow.__file__).parent / "web" / "vendor"
    manifest = json.loads((vendor / "VENDOR.json").read_text())
    for name, meta in manifest["files"].items():
        assert hashlib.sha256((vendor / name).read_bytes()).hexdigest() == meta["sha256"], name


def test_deleting_a_sample_removes_its_events(client, workspace):
    state = client.app.state.lymphflow
    s = workspace["samples"][0]
    counts(client, s["id"])
    parquet = state.settings.events_dir / f"{s['sha256']}.parquet"
    assert parquet.exists() and list((state.settings.masks_dir / s["sha256"][:2] / s["sha256"]).glob("*.bits"))
    assert client.delete(f"/api/samples/{s['id']}").status_code == 200
    assert not parquet.exists() and not (state.settings.masks_dir / s["sha256"][:2] / s["sha256"]).exists()
    assert client.get(f"/api/samples/{s['id']}").status_code == 404
