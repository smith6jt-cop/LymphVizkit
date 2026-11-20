"""Scales and server-binned plots (density, histogram)."""
from __future__ import annotations

import numpy as np
from fastapi import APIRouter, Body, HTTPException, Response

from ..core import gates as gates_mod
from ..core import hist as hist_mod
from ..core import scales as scales_mod
from ..core.engine import GateNode, SampleData
from ..core.scales import Scale
from ..state import AppState, NotFound
from .common import State, User

router = APIRouter()
EDGE_SEGMENTS = 16  # per polygon edge when an outline has to be re-projected between scales


# ---- scales ----------------------------------------------------------------------------

@router.put("/experiments/{exp_id}/scales")
def set_scale(exp_id: int, payload: dict = Body(...), state: AppState = State, user: str = User):
    """Change a channel's *display* scale. Existing gates keep the scale they were drawn on."""
    state.experiment(exp_id, user)
    pnn = str(payload.get("pnn") or "")
    if state.channel_meta(exp_id, pnn) is None:
        raise NotFound(f"no sample in this experiment has channel '{pnn}'")
    scale = scales_mod.make_scale(str(payload.get("kind") or ""), payload.get("params") or {})
    sid = state.set_channel_scale(exp_id, pnn, scale)
    state.db.audit(user, "scale.set", experiment_id=exp_id, pnn=pnn, scale=scale.to_json())
    return {"pnn": pnn, "scale_id": sid, "scale": scale.to_json()}


@router.post("/scales/apply")
def apply_scale(payload: dict = Body(...), state: AppState = State, user: str = User):
    """Forward (raw -> scale units) or inverse transform of a few values."""
    scale = state.scale(int(payload["scale_id"]))
    values = np.asarray(payload.get("values") or [], dtype=np.float64)
    if values.size > 10_000:
        raise HTTPException(422, "too many values")
    fn = scales_mod.inverse if payload.get("inverse") else scales_mod.forward
    out = fn(scale, values)
    return {"values": [None if not np.isfinite(v) else float(v) for v in out]}


# ---- helpers ---------------------------------------------------------------------------

def _axis(state: AppState, exp_id: int, sdata: SampleData, pnn: str,
          lo: float | None, hi: float | None) -> dict:
    ch = sdata.channel(pnn)
    sid, scale = state.channel_scale(exp_id, pnn)
    meta = state.channel_meta(exp_id, pnn)
    dlo, dhi = scales_mod.default_window(scale, ch.kind, ch.pnr, meta["exp_vmin"], meta["exp_vmax"])
    wlo = dlo if lo is None else float(lo)
    whi = dhi if hi is None else float(hi)
    if not (np.isfinite(wlo) and np.isfinite(whi)) or whi <= wlo:
        raise HTTPException(422, f"invalid display window for {pnn}")
    return {"pnn": pnn, "pns": ch.pns, "kind": ch.kind, "scale_id": sid, "scale": scale,
            "window": (wlo, whi), "default_window": (dlo, dhi)}


def _axis_json(ax: dict) -> dict:
    sc: Scale = ax["scale"]
    return {"pnn": ax["pnn"], "pns": ax["pns"], "kind": ax["kind"], "scale_id": ax["scale_id"],
            "scale": sc.to_json(), "window": list(ax["window"]), "default_window": list(ax["default_window"]),
            "ticks": scales_mod.ticks(sc, *ax["window"])}


def _convert(values: np.ndarray, src: Scale, dst: Scale) -> np.ndarray:
    if src.hash == dst.hash:
        return values
    return scales_mod.forward(dst, scales_mod.inverse(src, values))


def _densify(points: np.ndarray, n: int) -> np.ndarray:
    closed = np.vstack((points, points[:1]))
    t = np.linspace(0.0, 1.0, n, endpoint=False)[:, None]
    segs = [closed[i] + t * (closed[i + 1] - closed[i]) for i in range(len(points))]
    return np.vstack(segs)


def _gate_overlays(state: AppState, sdata: SampleData, nodes: dict[int, GateNode],
                   gate_dicts: list[dict], parent_id: int, parent_count: int,
                   xax: dict, yax: dict | None) -> list[dict]:
    """Child gates of `parent_id` drawable on these axes, projected into display units."""
    out = []
    for g in gate_dicts:
        if int(g["parent_id"] or 0) != parent_id:
            continue
        pnns = [d["pnn"] for d in g["dims"]]
        node = nodes[g["id"]]
        dims = node.spec.dims
        if len(pnns) == 2:
            if yax is None or {pnns[0], pnns[1]} != {xax["pnn"], yax["pnn"]}:
                continue
            swap = pnns[0] != xax["pnn"]
            disp = (yax, xax) if swap else (xax, yax)   # display axis for gate dim 0, dim 1
        else:
            if pnns[0] == xax["pnn"]:
                swap, disp = False, (xax, yax)
            elif yax is not None and pnns[0] == yax["pnn"]:
                swap, disp = True, (yax, xax)
            else:
                continue

        mismatch = any(disp[i] is not None and dims[i].scale.hash != disp[i]["scale"].hash
                       for i in range(len(dims)))
        # Display window expressed in the gate's own units (used to close unbounded sides).
        win = []
        for i in range(2):
            ax = disp[i] if i < len(disp) else None
            if ax is None:
                win += [0.0, 1.0]
            elif i < len(dims):
                w = _convert(np.asarray(ax["window"], dtype=np.float64), ax["scale"], dims[i].scale)
                win += [float(w[0]), float(w[1])]
            else:  # range gate: the other display axis is not a gate dimension
                win += [float(ax["window"][0]), float(ax["window"][1])]
        pts = np.asarray(gates_mod.outline(node.spec.kind, node.spec.geom, tuple(win)), dtype=np.float64)
        if mismatch:
            # Edge-extended vertices lie far outside the window; keep them within one window
            # span so the inverse transform stays finite when re-projecting.
            for i in range(len(dims)):
                span = abs(win[2 * i + 1] - win[2 * i])
                lo, hi = min(win[2 * i], win[2 * i + 1]) - span, max(win[2 * i], win[2 * i + 1]) + span
                pts[:, i] = np.clip(pts[:, i], lo, hi)
            if node.spec.kind != "ellipse":
                pts = _densify(pts, EDGE_SEGMENTS)
        for i in range(len(dims)):
            if disp[i] is not None:
                pts[:, i] = _convert(pts[:, i], dims[i].scale, disp[i]["scale"])
        if swap:
            pts = pts[:, ::-1]
        entry = {"id": g["id"], "name": g["name"], "kind": g["kind"],
                 "outline": [[float(a), float(b)] for a, b in pts if np.isfinite(a) and np.isfinite(b)],
                 "scale_mismatch": bool(mismatch), "overridden": node.overridden}
        count = state.engine.count(sdata, node)
        entry["count"] = count
        entry["pct_parent"] = (100.0 * count / parent_count) if parent_count else None
        out.append(entry)
    return out


def _context(state: AppState, sample_id: int, user: str, parent: int):
    row = state.sample(sample_id, user)
    exp_id = row["experiment_id"]
    sdata = state.sample_data(row)
    gate_dicts = state.gate_dicts(exp_id)
    from .. import snapshot as snap

    nodes = snap.nodes_for_sample(gate_dicts, sample_id)
    node = None
    if parent:
        if parent not in nodes:
            raise NotFound(f"population {parent} not found")
        node = nodes[parent]
    return row, exp_id, sdata, gate_dicts, nodes, node


def _frame(header: dict, counts: np.ndarray) -> Response:
    return Response(content=hist_mod.pack_frame(header, counts), media_type="application/octet-stream",
                    headers={"Cache-Control": "no-store"})


# ---- plots -----------------------------------------------------------------------------

@router.get("/samples/{sample_id}/plots/density")
def density(sample_id: int, x: str, y: str, parent: int = 0, nb: int = 256,
            xmin: float | None = None, xmax: float | None = None,
            ymin: float | None = None, ymax: float | None = None,
            state: AppState = State, user: str = User):
    if nb not in (64, 128, 256, 512):
        raise HTTPException(422, "nb must be 64, 128, 256 or 512")
    if x == y:
        raise HTTPException(422, "x and y must be different channels")
    row, exp_id, sdata, gate_dicts, nodes, node = _context(state, sample_id, user, parent)
    sdata.require([x, y])
    xax = _axis(state, exp_id, sdata, x, xmin, xmax)
    yax = _axis(state, exp_id, sdata, y, ymin, ymax)
    mask = state.engine.mask(sdata, node)
    xv, yv = sdata.scaled(x, xax["scale"]), sdata.scaled(y, yax["scale"])
    counts, n_shown, n_nonfinite = hist_mod.hist2d(xv, yv, mask, xax["window"], yax["window"], nb)
    parent_count = sdata.n_events if mask is None else int(np.count_nonzero(mask))
    header = {
        "type": "density", "nb": nb, "ny": nb, "sample_id": sample_id, "parent": parent,
        "parent_name": node.name if node else "All events",
        "n_parent": parent_count, "n_shown": n_shown, "n_nonfinite": n_nonfinite,
        "max_count": int(counts.max()) if counts.size else 0,
        "x": _axis_json(xax), "y": _axis_json(yax),
        "off_scale": {"x": hist_mod.off_scale(xv, mask, xax["window"]),
                      "y": hist_mod.off_scale(yv, mask, yax["window"])},
        "comp": {"state": sdata.comp_state, "applied": sdata.comp_active},
        "gates": _gate_overlays(state, sdata, nodes, gate_dicts, parent, parent_count, xax, yax),
    }
    return _frame(header, counts)


@router.get("/samples/{sample_id}/plots/histogram")
def histogram(sample_id: int, x: str, parent: int = 0, nb: int = 256,
              xmin: float | None = None, xmax: float | None = None,
              state: AppState = State, user: str = User):
    if nb not in (64, 128, 256, 512):
        raise HTTPException(422, "nb must be 64, 128, 256 or 512")
    row, exp_id, sdata, gate_dicts, nodes, node = _context(state, sample_id, user, parent)
    sdata.require([x])
    xax = _axis(state, exp_id, sdata, x, xmin, xmax)
    mask = state.engine.mask(sdata, node)
    xv = sdata.scaled(x, xax["scale"])
    counts, n_shown, n_nonfinite = hist_mod.hist1d(xv, mask, xax["window"], nb)
    parent_count = sdata.n_events if mask is None else int(np.count_nonzero(mask))
    header = {
        "type": "histogram", "nb": nb, "ny": 1, "sample_id": sample_id, "parent": parent,
        "parent_name": node.name if node else "All events",
        "n_parent": parent_count, "n_shown": n_shown, "n_nonfinite": n_nonfinite,
        "max_count": int(counts.max()) if counts.size else 0,
        "x": _axis_json(xax), "y": None,
        "off_scale": {"x": hist_mod.off_scale(xv, mask, xax["window"]), "y": None},
        "comp": {"state": sdata.comp_state, "applied": sdata.comp_active},
        "gates": _gate_overlays(state, sdata, nodes, gate_dicts, parent, parent_count, xax, None),
    }
    return _frame(header, counts)
