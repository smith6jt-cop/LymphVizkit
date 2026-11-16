"""Gating template (tree of gates), per-sample overrides, populations, statistics, exports."""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Body, HTTPException, Response

from .. import snapshot as snap
from ..config import ENGINE_VERSION
from ..core import gates as gates_mod
from ..core.engine import CompensationInvalid, PanelMismatch, make_dim
from ..db import utcnow
from ..state import AppState, Conflict, NotFound
from .common import State, User

router = APIRouter()


def _check_revision(exp, payload: dict) -> None:
    expected = payload.get("expected_revision")
    if expected is not None and int(expected) != exp["revision"]:
        raise Conflict(f"the gating template changed (revision {exp['revision']}, expected {expected}); reload")


def _sibling_name_free(state: AppState, exp_id: int, parent_id: int | None, name: str, ignore: int = 0) -> None:
    row = state.db.one(
        "SELECT id FROM gates WHERE experiment_id=? AND IFNULL(parent_id,0)=? AND name=? AND id<>?",
        (exp_id, parent_id or 0, name, ignore))
    if row is not None:
        raise Conflict(f"a sibling population named '{name}' already exists")


def _resolve_dims(state: AppState, exp_id: int, dims_in: list) -> tuple[list[dict], list]:
    """Bind each dimension to an immutable scale row (the snapshot the gate was drawn on)."""
    stored, objs = [], []
    for d in dims_in:
        if isinstance(d, str):
            d = {"pnn": d}
        pnn = str(d.get("pnn") or "")
        if state.channel_meta(exp_id, pnn) is None:
            raise NotFound(f"no sample in this experiment has channel '{pnn}'")
        scale_id = int(d["scale_id"]) if d.get("scale_id") is not None else state.channel_scale(exp_id, pnn)[0]
        comp = d.get("comp", "sample")
        objs.append(make_dim(pnn, state.scale(scale_id), comp))
        stored.append({"pnn": pnn, "scale_id": scale_id, "comp": comp})
    return stored, objs


@router.get("/experiments/{exp_id}/gates")
def list_gates(exp_id: int, state: AppState = State, user: str = User):
    exp = state.experiment(exp_id, user)
    return {"revision": exp["revision"], "gates": snap.depth_first(state.gate_dicts(exp_id))}


@router.post("/experiments/{exp_id}/gates")
def create_gate(exp_id: int, payload: dict = Body(...), state: AppState = State, user: str = User):
    exp = state.experiment(exp_id, user)
    _check_revision(exp, payload)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "gate name is required")
    parent_id = int(payload.get("parent_id") or 0) or None
    if parent_id is not None and state.gate(parent_id, user)["experiment_id"] != exp_id:
        raise NotFound("parent population not found in this experiment")
    kind = str(payload.get("kind") or "")
    stored, objs = _resolve_dims(state, exp_id, payload.get("dims") or [])
    spec = gates_mod.make_spec(kind, objs, payload.get("geom") or {})
    _sibling_name_free(state, exp_id, parent_id, name)
    now = utcnow()
    cur = state.db.execute(
        "INSERT INTO gates(experiment_id, parent_id, name, kind, dims, geom, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (exp_id, parent_id, name[:120], kind, json.dumps(stored), spec.geom_json, now, now))
    revision = state.bump_revision(exp_id)
    state.db.audit(user, "gate.create", experiment_id=exp_id, gate_id=cur.lastrowid, name=name, kind=kind,
                   dims=stored, geom=spec.geom, revision=revision)
    return {"id": cur.lastrowid, "revision": revision}


@router.put("/gates/{gate_id}")
def update_gate(gate_id: int, payload: dict = Body(...), state: AppState = State, user: str = User):
    g = state.gate(gate_id, user)
    exp = state.experiment(g["experiment_id"], user)
    _check_revision(exp, payload)
    name = g["name"]
    if "name" in payload:
        name = str(payload["name"]).strip()
        if not name:
            raise HTTPException(422, "gate name cannot be empty")
        _sibling_name_free(state, exp["id"], g["parent_id"], name, ignore=gate_id)
    geom_json = g["geom"]
    dims_json = g["dims"]
    rebound = False
    if "dims" in payload:
        # Explicit re-binding to another scale: an audited action, never a side effect.
        stored, objs = _resolve_dims(state, exp["id"], payload["dims"])
        if [d["pnn"] for d in stored] != [d["pnn"] for d in json.loads(g["dims"])]:
            raise HTTPException(422, "a gate's channels cannot be changed; delete it and draw a new one")
        dims_json, rebound = json.dumps(stored), True
        if "geom" not in payload:
            raise HTTPException(422, "re-binding a gate to another scale requires its geometry in that scale")
    if "geom" in payload:
        stored = json.loads(dims_json)
        objs = [make_dim(d["pnn"], state.scale(d["scale_id"]), d.get("comp", "sample")) for d in stored]
        geom_json = gates_mod.make_spec(g["kind"], objs, payload["geom"]).geom_json
    state.db.execute("UPDATE gates SET name=?, geom=?, dims=?, updated_at=? WHERE id=?",
                     (name[:120], geom_json, dims_json, utcnow(), gate_id))
    revision = state.bump_revision(exp["id"])
    state.db.audit(user, "gate.rebind" if rebound else "gate.update", gate_id=gate_id, name=name,
                   geom=json.loads(geom_json), revision=revision)
    return {"id": gate_id, "revision": revision}


@router.delete("/gates/{gate_id}")
def delete_gate(gate_id: int, state: AppState = State, user: str = User):
    g = state.gate(gate_id, user)
    state.db.execute("DELETE FROM gates WHERE id=?", (gate_id,))   # children cascade
    revision = state.bump_revision(g["experiment_id"])
    state.db.audit(user, "gate.delete", gate_id=gate_id, name=g["name"], revision=revision)
    return {"deleted": gate_id, "revision": revision}


@router.put("/gates/{gate_id}/overrides/{sample_id}")
def set_override(gate_id: int, sample_id: int, payload: dict = Body(...), state: AppState = State, user: str = User):
    """Per-sample geometry for a template gate (same kind, same dimensions)."""
    g = state.gate(gate_id, user)
    s = state.sample(sample_id, user)
    if s["experiment_id"] != g["experiment_id"]:
        raise NotFound("sample is not in this gate's experiment")
    stored = json.loads(g["dims"])
    objs = [make_dim(d["pnn"], state.scale(d["scale_id"]), d.get("comp", "sample")) for d in stored]
    geom_json = gates_mod.make_spec(g["kind"], objs, payload.get("geom") or {}).geom_json
    state.db.execute(
        "INSERT INTO gate_overrides(gate_id, sample_id, geom, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(gate_id, sample_id) DO UPDATE SET geom=excluded.geom, updated_at=excluded.updated_at",
        (gate_id, sample_id, geom_json, utcnow()))
    revision = state.bump_revision(g["experiment_id"])
    state.db.audit(user, "gate.override", gate_id=gate_id, sample_id=sample_id, geom=json.loads(geom_json))
    return {"gate_id": gate_id, "sample_id": sample_id, "revision": revision}


@router.delete("/gates/{gate_id}/overrides/{sample_id}")
def clear_override(gate_id: int, sample_id: int, state: AppState = State, user: str = User):
    g = state.gate(gate_id, user)
    state.sample(sample_id, user)
    state.db.execute("DELETE FROM gate_overrides WHERE gate_id=? AND sample_id=?", (gate_id, sample_id))
    revision = state.bump_revision(g["experiment_id"])
    state.db.audit(user, "gate.override.clear", gate_id=gate_id, sample_id=sample_id)
    return {"gate_id": gate_id, "sample_id": sample_id, "revision": revision}


# ---- populations / statistics ----------------------------------------------------------

def _populations(state: AppState, row) -> dict:
    """Counts for every population of one sample. Errors are reported per sample, never hidden."""
    exp_id = row["experiment_id"]
    gate_dicts = snap.depth_first(state.gate_dicts(exp_id))
    result = {"sample_id": row["id"], "sample": row["name"], "n_events": row["n_events"], "error": None,
              "populations": []}
    sdata = state.sample_data(row)
    try:
        sdata.require_usable_comp()
    except CompensationInvalid as exc:
        result["error"] = {"kind": "compensation_invalid", "detail": str(exc)}
        return result
    nodes = snap.nodes_for_sample(gate_dicts, row["id"])
    failed: set[int] = set()
    for g in gate_dicts:
        node = nodes[g["id"]]
        entry = {"id": g["id"], "parent_id": g["parent_id"] or 0, "name": g["name"], "kind": g["kind"],
                 "depth": g["depth"], "path": " / ".join(node.path),
                 "dims": [{"pnn": d["pnn"], "scale_id": d["scale_id"]} for d in g["dims"]],
                 "overridden": node.overridden, "count": None, "pct_parent": None, "pct_total": None,
                 "error": None}
        if (g["parent_id"] or 0) in failed:
            entry["error"] = "parent population could not be evaluated"
            failed.add(g["id"])
        else:
            try:
                count = state.engine.count(sdata, node)
                parent_count = state.engine.count(sdata, node.parent)
                entry.update(count=count,
                             pct_parent=(100.0 * count / parent_count) if parent_count else None,
                             pct_total=(100.0 * count / sdata.n_events) if sdata.n_events else None)
            except PanelMismatch as exc:
                entry["error"] = str(exc)
                failed.add(g["id"])
        result["populations"].append(entry)
    return result


@router.get("/samples/{sample_id}/populations")
def populations(sample_id: int, state: AppState = State, user: str = User):
    return _populations(state, state.sample(sample_id, user))


@router.get("/samples/{sample_id}/stats")
def stats(sample_id: int, gate: int = 0, state: AppState = State, user: str = User):
    row = state.sample(sample_id, user)
    sdata = state.sample_data(row)
    node = None
    if gate:
        nodes = state.nodes(row["experiment_id"], sample_id)
        if gate not in nodes:
            raise NotFound(f"population {gate} not found")
        node = nodes[gate]
    result = state.stats(row, sdata, gate, node)
    labels = {c.pnn: c.pns for c in sdata.channels}
    return {**result, "sample_id": sample_id, "gate_id": gate,
            "population": " / ".join(node.path) if node else "All events", "stains": labels}


CSV_COLUMNS = ["sample", "group", "population", "count", "parent_count", "pct_parent", "pct_total",
               "channel", "marker", "n", "median", "mean", "sd", "cv", "rcv", "geomean", "n_nonpositive",
               "values", "comp_state", "comp_hash", "gate_overridden", "error"]


@router.get("/experiments/{exp_id}/stats.csv")
def stats_csv(exp_id: int, state: AppState = State, user: str = User):
    """Long-format statistics for every sample x population x channel."""
    exp = state.experiment(exp_id, user)
    buf = io.StringIO()
    for line in (
        f"# lymphflow statistics export, engine version {ENGINE_VERSION}",
        f"# experiment: {exp['name']} (id {exp['id']}), gating template revision {exp['revision']}",
        f"# generated: {utcnow()} by {user}",
        "# median/mean/sd/cv/rcv/geomean are computed on LINEAR values (compensated where 'values' says so),",
        "#   never on display-scaled values. geomean uses strictly positive values only; n_nonpositive counts the rest.",
        "# pct_parent = 100 * count / parent_count; pct_total = 100 * count / all events in the file.",
        "# gate_overridden = TRUE when this sample uses its own geometry for the gate or an ancestor.",
        "# A non-empty 'error' means the population could not be evaluated for that sample (it was not skipped silently).",
    ):
        buf.write(line + "\n")
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    w.writeheader()
    gate_dicts = snap.depth_first(state.gate_dicts(exp_id))
    for row in state.db.all("SELECT * FROM samples WHERE experiment_id=? ORDER BY id", (exp_id,)):
        base = {"sample": row["name"], "group": row["group_label"]}
        sdata = state.sample_data(row)
        try:
            sdata.require_usable_comp()
        except CompensationInvalid as exc:
            w.writerow({**base, "population": "All events", "error": str(exc)})
            continue
        nodes = snap.nodes_for_sample(gate_dicts, row["id"])
        stains = {c.pnn: c.pns for c in sdata.channels}
        for gid, node in [(0, None)] + [(g["id"], nodes[g["id"]]) for g in gate_dicts]:
            pop = " / ".join(node.path) if node else "All events"
            try:
                st = state.stats(row, sdata, gid, node)
            except PanelMismatch as exc:
                w.writerow({**base, "population": pop, "error": str(exc)})
                continue
            common = {**base, "population": pop, "count": st["count"], "parent_count": st["parent_count"],
                      "pct_parent": st["pct_parent"], "pct_total": st["pct_total"], "values": st["values"],
                      "comp_state": st["comp_state"], "comp_hash": st["comp_hash"][:12],
                      "gate_overridden": "TRUE" if st["overridden"] else "FALSE", "error": ""}
            for pnn, cs in st["channels"].items():
                w.writerow({**common, "channel": pnn, "marker": stains.get(pnn, ""), "n": cs["n"],
                            "median": cs["median"], "mean": cs["mean"], "sd": cs["sd"], "cv": cs["cv"],
                            "rcv": cs["rcv"], "geomean": cs["geomean"], "n_nonpositive": cs["n_nonpositive"]})
    state.db.audit(user, "stats.export", experiment_id=exp_id, revision=exp["revision"])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="lymphflow_stats_exp{exp_id}.csv"'})


@router.get("/experiments/{exp_id}/summary")
def summary(exp_id: int, gate: int = 0, metric: str = "pct_parent", channel: str = "",
            state: AppState = State, user: str = User):
    """One value per sample for a population, with group labels, for a grouped plot."""
    state.experiment(exp_id, user)
    if metric not in ("pct_parent", "pct_total", "count", "median"):
        raise HTTPException(422, "metric must be pct_parent, pct_total, count or median")
    if metric == "median" and not channel:
        raise HTTPException(422, "metric=median needs a channel")
    gate_dicts = state.gate_dicts(exp_id)
    if gate and gate not in {g["id"] for g in gate_dicts}:
        raise NotFound(f"population {gate} not found")
    rows = []
    for row in state.db.all("SELECT * FROM samples WHERE experiment_id=? ORDER BY id", (exp_id,)):
        entry = {"sample_id": row["id"], "sample": row["name"], "group": row["group_label"], "value": None,
                 "overridden": False, "error": None}
        try:
            sdata = state.sample_data(row)
            sdata.require_usable_comp()
            node = snap.nodes_for_sample(gate_dicts, row["id"])[gate] if gate else None
            entry["overridden"] = bool(node and any(n.overridden for n in _chain(node)))
            if metric == "median":
                st = state.stats(row, sdata, gate, node)
                if channel not in st["channels"]:
                    raise PanelMismatch(row["name"], [channel])
                entry["value"] = st["channels"][channel]["median"]
            else:
                count = state.engine.count(sdata, node)
                if metric == "count":
                    entry["value"] = count
                elif metric == "pct_total":
                    entry["value"] = 100.0 * count / sdata.n_events if sdata.n_events else None
                else:
                    parent = state.engine.count(sdata, node.parent if node else None)
                    entry["value"] = 100.0 * count / parent if parent else None
        except (PanelMismatch, CompensationInvalid) as exc:
            entry["error"] = str(exc)
        rows.append(entry)
    return {"metric": metric, "channel": channel, "gate": gate, "rows": rows}


def _chain(node):
    while node is not None:
        yield node
        node = node.parent


@router.get("/experiments/{exp_id}/gatingml")
def gatingml(exp_id: int, sample_id: int = 0, state: AppState = State, user: str = User):
    """GatingML 2.0 export of the template (or of one sample's resolved gates)."""
    exp = state.experiment(exp_id, user)
    try:
        from ..interop import gatingml as gml
    except ImportError as exc:  # optional 'interop' extra (FlowKit) not installed
        raise HTTPException(501, f"GatingML export needs the optional 'interop' extra: {exc}") from exc
    gate_dicts = snap.depth_first(state.gate_dicts(exp_id))
    if sample_id:
        state.sample(sample_id, user)
    # Channels the samples' own (embedded, enabled) matrices compensate -> compensation-ref "FCS".
    detectors: set[str] = set()
    for m in state.db.all(
        "SELECT DISTINCT m.detectors FROM samples s JOIN comp_matrices m ON m.id = s.comp_matrix_id "
        "WHERE s.experiment_id=? AND s.comp_state='embedded' AND s.comp_enabled=1", (exp_id,)):
        detectors.update(json.loads(m["detectors"]))
    xml, id_map = gml.export_gatingml(gate_dicts, sample_id or None, detectors)
    state.db.audit(user, "gatingml.export", experiment_id=exp_id, revision=exp["revision"], sample_id=sample_id)
    return Response(content=xml, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="lymphflow_exp{exp_id}_gates.xml"',
                             "X-Lymphflow-Gate-Ids": json.dumps(id_map)})
