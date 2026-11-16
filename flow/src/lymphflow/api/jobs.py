"""Background jobs: submit, poll, cancel. The browser polls; nothing depends on WebSockets."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from ..state import AppState, NotFound
from .common import State, User

router = APIRouter()
JOB_TYPES = ("apply_template", "flowsom")


def _job_row(state: AppState, job_id: str, user: str):
    row = state.db.one("SELECT * FROM jobs WHERE id=? AND owner_id=?", (job_id, user))
    if row is None:
        raise NotFound(f"job {job_id} not found")
    return row


def _flowsom_params(state: AppState, exp_id: int, payload: dict) -> dict:
    pnns = [str(p) for p in (payload.get("channels") or [])]
    if len(pnns) < 2:
        raise HTTPException(422, "choose at least two channels for FlowSOM")
    channels = []
    for pnn in pnns:
        meta = state.channel_meta(exp_id, pnn)
        if meta is None:
            raise NotFound(f"no sample in this experiment has channel '{pnn}'")
        _, scale = state.channel_scale(exp_id, pnn)   # snapshot of the current display scale
        channels.append({"pnn": pnn, "pns": meta["pns"], "scale": {"kind": scale.kind, "params": scale.p}})
    gate_id = int(payload.get("gate_id") or 0)
    if gate_id and state.db.one("SELECT 1 FROM gates WHERE id=? AND experiment_id=?", (gate_id, exp_id)) is None:
        raise NotFound(f"population {gate_id} not found")

    def bounded(name: str, default: int, lo: int, hi: int) -> int:
        v = int(payload.get(name, default))
        if not lo <= v <= hi:
            raise HTTPException(422, f"{name} must be between {lo} and {hi}")
        return v

    return {"gate_id": gate_id, "channels": channels,
            "n_per_sample": bounded("n_per_sample", 20000, 100, 200000),
            "xdim": bounded("xdim", 10, 2, 20), "ydim": bounded("ydim", 10, 2, 20),
            "n_clusters": bounded("n_clusters", 10, 2, 40), "seed": bounded("seed", 42, 0, 2**31 - 1)}


@router.post("/experiments/{exp_id}/jobs")
def submit(exp_id: int, payload: dict = Body(...), state: AppState = State, user: str = User):
    state.experiment(exp_id, user)
    job_type = str(payload.get("type") or "")
    if job_type not in JOB_TYPES:
        raise HTTPException(422, f"job type must be one of {', '.join(JOB_TYPES)}")
    if state.db.one("SELECT 1 FROM samples WHERE experiment_id=?", (exp_id,)) is None:
        raise HTTPException(422, "the experiment has no samples")
    params = _flowsom_params(state, exp_id, payload) if job_type == "flowsom" else {}
    job_id = state.dispatcher.submit(exp_id, user, job_type, params)
    state.db.audit(user, "job.submit", job_id=job_id, type=job_type, experiment_id=exp_id, params=params)
    return state.dispatcher.describe(_job_row(state, job_id, user))


@router.get("/experiments/{exp_id}/jobs")
def list_jobs(exp_id: int, state: AppState = State, user: str = User):
    state.experiment(exp_id, user)
    rows = state.db.all("SELECT * FROM jobs WHERE experiment_id=? ORDER BY created_at DESC, id DESC LIMIT 50",
                        (exp_id,))
    return [state.dispatcher.describe(r) for r in rows]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, state: AppState = State, user: str = User):
    return state.dispatcher.describe(_job_row(state, job_id, user))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, state: AppState = State, user: str = User):
    row = _job_row(state, job_id, user)
    if row["state"] in ("queued", "running"):
        state.dispatcher.cancel(job_id)
        state.db.audit(user, "job.cancel", job_id=job_id)
    return state.dispatcher.describe(_job_row(state, job_id, user))
