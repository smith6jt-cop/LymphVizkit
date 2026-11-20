"""Experiments, samples (upload / folder registration), compensation, sample metadata."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, File, HTTPException, UploadFile

from ..core import fcs_ingest
from ..db import utcnow
from ..state import AppState, Conflict
from .common import State, User

router = APIRouter()
CHUNK = 1 << 20


def _sample_public(state: AppState, row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "n_events": row["n_events"], "n_params": row["n_params"],
        "n_nonfinite": row["n_nonfinite"], "fcs_version": row["fcs_version"], "dtype": row["dtype"],
        "comp_state": row["comp_state"], "comp_enabled": bool(row["comp_enabled"]),
        "comp_message": row["comp_message"], "group_label": row["group_label"],
        "sha256": row["sha256"], "created_at": row["created_at"],
    }


@router.get("/health")
def health(state: AppState = State):
    return {"status": "ok", "schema_version": state.schema_version, "auth_mode": state.settings.auth_mode}


@router.get("/me")
def me(state: AppState = State, user: str = User):
    return {"user": user, "auth_mode": state.settings.auth_mode,
            "max_upload_mb": state.settings.max_upload_mb,
            "folder_registration": bool(state.settings.register_roots)}


@router.get("/experiments")
def list_experiments(state: AppState = State, user: str = User):
    rows = state.db.all(
        "SELECT e.*, (SELECT COUNT(*) FROM samples s WHERE s.experiment_id=e.id) AS n_samples "
        "FROM experiments e WHERE owner_id=? ORDER BY id DESC", (user,))
    return [{"id": r["id"], "name": r["name"], "revision": r["revision"], "n_samples": r["n_samples"],
             "created_at": r["created_at"]} for r in rows]


@router.post("/experiments")
def create_experiment(payload: dict = Body(...), state: AppState = State, user: str = User):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "experiment name is required")
    cur = state.db.execute(
        "INSERT INTO experiments(name, owner_id, created_at) VALUES (?,?,?)", (name[:200], user, utcnow()))
    state.db.audit(user, "experiment.create", experiment_id=cur.lastrowid, name=name)
    return {"id": cur.lastrowid, "name": name, "revision": 0}


@router.delete("/experiments/{exp_id}")
def delete_experiment(exp_id: int, state: AppState = State, user: str = User):
    state.experiment(exp_id, user)
    rows = state.db.all("SELECT * FROM samples WHERE experiment_id=?", (exp_id,))
    state.db.execute("DELETE FROM experiments WHERE id=?", (exp_id,))
    for r in rows:
        state.delete_sample_files(r)
    state.db.audit(user, "experiment.delete", experiment_id=exp_id)
    return {"deleted": exp_id}


@router.get("/experiments/{exp_id}")
def get_experiment(exp_id: int, state: AppState = State, user: str = User):
    exp = state.experiment(exp_id, user)
    samples = state.db.all("SELECT * FROM samples WHERE experiment_id=? ORDER BY id", (exp_id,))
    chan_rows = state.db.all(
        "SELECT c.pnn, c.kind, MIN(c.idx) AS idx, COUNT(*) AS n_samples, "
        "GROUP_CONCAT(DISTINCT c.pns) AS stains "
        "FROM channels c JOIN samples s ON s.id=c.sample_id WHERE s.experiment_id=? "
        "GROUP BY c.pnn, c.kind ORDER BY MIN(c.idx)", (exp_id,))
    channels = []
    for c in chan_rows:
        sid, scale = state.channel_scale(exp_id, c["pnn"])
        stains = [s for s in (c["stains"] or "").split(",") if s]
        channels.append({"pnn": c["pnn"], "pns": stains[0] if len(stains) == 1 else "/".join(stains),
                         "kind": c["kind"], "n_samples": c["n_samples"],
                         "scale_id": sid, "scale": scale.to_json()})
    return {"id": exp["id"], "name": exp["name"], "revision": exp["revision"],
            "samples": [_sample_public(state, r) for r in samples], "channels": channels}


# ---- ingest ----------------------------------------------------------------------------

def _register(state: AppState, exp_id: int, user: str, fcs_path: Path, display_name: str) -> dict:
    token = uuid.uuid4().hex
    staging = state.settings.events_dir / f"{token}.parquet"
    try:
        result = fcs_ingest.ingest_fcs(fcs_path, staging, keep_times=state.settings.keep_acquisition_times)
    except Exception:
        staging.unlink(missing_ok=True)
        staging.with_suffix(".tmp").unlink(missing_ok=True)
        raise
    # Content-addressed event file: identical FCS files share one Parquet.
    final_name = f"{result.sha256}.parquet"
    final = state.settings.events_dir / final_name
    if final.exists():
        staging.unlink(missing_ok=True)
    else:
        staging.replace(final)

    with state.db.transaction() as c:
        comp_id = None
        if result.comp is not None:
            cm = result.comp
            c.execute("INSERT OR IGNORE INTO comp_matrices(hash, detectors, matrix, source) VALUES (?,?,?,?)",
                      (cm.hash, json.dumps(list(cm.detectors)), json.dumps(cm.matrix.tolist()), "embedded"))
            comp_id = c.execute("SELECT id FROM comp_matrices WHERE hash=?", (cm.hash,)).fetchone()["id"]
        cur = c.execute(
            "INSERT INTO samples(experiment_id, name, sha256, fcs_version, n_events, n_params, n_nonfinite, "
            "parquet_file, dtype, comp_state, comp_matrix_id, comp_enabled, comp_message, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (exp_id, display_name[:200], result.sha256, result.fcs_version, result.n_events, result.n_params,
             result.n_nonfinite, final_name, result.dtype, result.comp_state, comp_id, 1,
             result.comp_message, utcnow()))
        sample_id = cur.lastrowid
        c.executemany(
            "INSERT INTO channels(sample_id, idx, col, pnn, pns, kind, pnr, vmin, vmax) VALUES (?,?,?,?,?,?,?,?,?)",
            [(sample_id, ch.idx, ch.col, ch.pnn, ch.pns, ch.kind, ch.pnr, ch.vmin, ch.vmax)
             for ch in result.channels])
        c.executemany("INSERT INTO sample_keywords(sample_id, key, value) VALUES (?,?,?)",
                      [(sample_id, k, v) for k, v in result.keywords.items()])
    state.db.audit(user, "sample.ingest", sample_id=sample_id, experiment_id=exp_id, sha256=result.sha256,
                   n_events=result.n_events, keywords_kept=len(result.keywords),
                   keywords_dropped=result.n_keywords_dropped, comp_state=result.comp_state)
    row = state.db.one("SELECT * FROM samples WHERE id=?", (sample_id,))
    return {**_sample_public(state, row), "warnings": result.warnings,
            "keywords_kept": len(result.keywords), "keywords_dropped": result.n_keywords_dropped}


@router.post("/experiments/{exp_id}/samples")
def upload_samples(exp_id: int, files: list[UploadFile] = File(...), state: AppState = State, user: str = User):
    """Upload one or more FCS files. The FCS file is parsed, converted and then deleted."""
    state.experiment(exp_id, user)
    limit = state.settings.max_upload_mb * 1024 * 1024
    results = []
    for up in files:
        display = Path(up.filename or "sample.fcs").name
        tmp = state.settings.incoming_dir / f"{uuid.uuid4().hex}.fcs"
        try:
            size = 0
            with open(tmp, "wb") as out:
                while chunk := up.file.read(CHUNK):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"{display}: larger than the {state.settings.max_upload_mb} MB limit")
                    out.write(chunk)
            try:
                results.append({"file": display, "ok": True,
                                "sample": _register(state, exp_id, user, tmp, Path(display).stem)})
            except fcs_ingest.FcsError as exc:
                results.append({"file": display, "ok": False, "error": str(exc)})
        finally:
            tmp.unlink(missing_ok=True)
    return {"results": results}


@router.post("/experiments/{exp_id}/register-folder")
def register_folder(exp_id: int, payload: dict = Body(...), state: AppState = State, user: str = User):
    """Ingest every *.fcs in a server-side folder under an allowed root (off unless configured)."""
    state.experiment(exp_id, user)
    roots = state.settings.register_roots
    if not roots:
        raise HTTPException(403, "server-side folder registration is not enabled on this deployment")
    folder = Path(str(payload.get("path") or "")).expanduser().resolve()
    if not any(folder == r or r in folder.parents for r in roots):
        raise HTTPException(403, "folder is outside the allowed roots")
    if not folder.is_dir():
        raise HTTPException(404, "folder not found")
    results = []
    for f in sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".fcs"):
        try:
            results.append({"file": f.name, "ok": True, "sample": _register(state, exp_id, user, f, f.stem)})
        except fcs_ingest.FcsError as exc:
            results.append({"file": f.name, "ok": False, "error": str(exc)})
    return {"results": results}


# ---- samples ---------------------------------------------------------------------------

@router.get("/samples/{sample_id}")
def get_sample(sample_id: int, state: AppState = State, user: str = User):
    row = state.sample(sample_id, user)
    d = state.sample_dict(row)
    keywords = {k["key"]: k["value"] for k in
                state.db.all("SELECT key, value FROM sample_keywords WHERE sample_id=? ORDER BY key", (sample_id,))}
    return {**_sample_public(state, row), "channels": d["channels"], "keywords": keywords}


@router.patch("/samples/{sample_id}")
def patch_sample(sample_id: int, payload: dict = Body(...), state: AppState = State, user: str = User):
    row = state.sample(sample_id, user)
    name = str(payload["name"]).strip()[:200] if "name" in payload else row["name"]
    group = str(payload["group_label"]).strip()[:100] if "group_label" in payload else row["group_label"]
    if not name:
        raise HTTPException(422, "sample name cannot be empty")
    state.db.execute("UPDATE samples SET name=?, group_label=? WHERE id=?", (name, group, sample_id))
    return _sample_public(state, state.sample(sample_id, user))


@router.delete("/samples/{sample_id}")
def delete_sample(sample_id: int, state: AppState = State, user: str = User):
    row = state.sample(sample_id, user)
    state.db.execute("DELETE FROM population_stats WHERE sample_id=?", (sample_id,))
    state.db.execute("DELETE FROM samples WHERE id=?", (sample_id,))
    state.delete_sample_files(row)
    state.db.audit(user, "sample.delete", sample_id=sample_id)
    return {"deleted": sample_id}


@router.get("/samples/{sample_id}/compensation")
def get_compensation(sample_id: int, state: AppState = State, user: str = User):
    row = state.sample(sample_id, user)
    d = state.sample_dict(row)
    return {"state": row["comp_state"], "enabled": bool(row["comp_enabled"]), "message": row["comp_message"],
            "applied": row["comp_state"] == "embedded" and bool(row["comp_enabled"]),
            "detectors": d["comp"]["detectors"] if d["comp"] else [],
            "matrix": d["comp"]["matrix"] if d["comp"] else [],
            "hash": d["comp"]["hash"] if d["comp"] else None}


@router.put("/samples/{sample_id}/compensation")
def set_compensation(sample_id: int, payload: dict = Body(...), state: AppState = State, user: str = User):
    """Switch the embedded matrix on or off for a sample. Raw events are never modified."""
    row = state.sample(sample_id, user)
    if "enabled" not in payload:
        raise HTTPException(422, "'enabled' is required")
    enabled = bool(payload["enabled"])
    if enabled and row["comp_state"] == "invalid":
        raise Conflict("this sample's embedded spillover matrix cannot be applied: " + (row["comp_message"] or ""))
    state.db.execute("UPDATE samples SET comp_enabled=? WHERE id=?", (1 if enabled else 0, sample_id))
    state.db.audit(user, "sample.compensation", sample_id=sample_id, enabled=enabled)
    return get_compensation(sample_id, state, user)
