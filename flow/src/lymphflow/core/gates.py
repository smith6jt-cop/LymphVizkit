"""Gate geometry: validation, membership, outlines.

Coordinates are in the scale units of each dimension (GatingML semantics). Membership uses
the FlowUtils primitives FlowKit uses, with GatingML boundary rules:

  rect / range   min inclusive, max exclusive; a missing bound is unbounded
  polygon        winding-number test, boundary inclusive (flowutils.gating.points_in_polygon)
  ellipse        Mahalanobis distance^2 <= distance_square (flowutils.gating.points_in_ellipsoid)

A NaN coordinate is never inside any gate.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

import numpy as np
from flowutils import gating as _fu_gating

from .scales import Scale

KINDS = {"rect": 2, "range": 1, "polygon": 2, "ellipse": 2}


class GateError(ValueError):
    pass


@dataclass(frozen=True)
class Dim:
    pnn: str
    scale: Scale
    comp: str = "sample"  # 'sample': use the sample's active compensation; 'none': raw values

    def key(self) -> list:
        return [self.pnn, self.scale.hash, self.comp]


@dataclass(frozen=True)
class GateSpec:
    kind: str
    dims: tuple[Dim, ...]
    geom_json: str  # canonical JSON of the validated geometry

    @property
    def geom(self) -> dict:
        return json.loads(self.geom_json)


def _f(value, what: str, allow_none: bool = False):
    if value is None:
        if allow_none:
            return None
        raise GateError(f"{what} is required")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise GateError(f"{what} is not a number") from None
    if not math.isfinite(v):
        raise GateError(f"{what} is not finite")
    return v


def validate_geom(kind: str, geom: dict) -> dict:
    """Return a normalised geometry dict or raise GateError."""
    if kind not in KINDS:
        raise GateError(f"unknown gate kind '{kind}'")
    if not isinstance(geom, dict):
        raise GateError("gate geometry must be an object")

    if kind in ("rect", "range"):
        axes = ("x", "y") if kind == "rect" else ("x",)
        out = {}
        for ax in axes:
            lo = _f(geom.get(f"{ax}0"), f"{ax}0", allow_none=True)
            hi = _f(geom.get(f"{ax}1"), f"{ax}1", allow_none=True)
            if lo is not None and hi is not None:
                if lo == hi:
                    raise GateError(f"{kind} gate has zero width on {ax}")
                lo, hi = min(lo, hi), max(lo, hi)
            out[f"{ax}0"], out[f"{ax}1"] = lo, hi
        # A missing bound means "unbounded on that side" (a gate dragged to the plot edge, so
        # that off-scale events piled on the edge are included). At least one bound must exist.
        if all(v is None for v in out.values()):
            raise GateError(f"{kind} gate needs at least one bound")
        return out

    if kind == "polygon":
        verts = geom.get("vertices")
        if not isinstance(verts, list) or len(verts) < 3:
            raise GateError("polygon gate needs at least 3 vertices")
        pts = [[_f(v[0], "vertex x"), _f(v[1], "vertex y")] for v in verts]
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        if len({tuple(p) for p in pts}) < 3:
            raise GateError("polygon gate needs at least 3 distinct vertices")
        return {"vertices": pts}

    # ellipse
    center = geom.get("center")
    cov = geom.get("cov")
    if not (isinstance(center, list) and len(center) == 2):
        raise GateError("ellipse gate needs a 2-element center")
    if not (isinstance(cov, list) and len(cov) == 2 and all(isinstance(r, list) and len(r) == 2 for r in cov)):
        raise GateError("ellipse gate needs a 2 x 2 covariance matrix")
    c = [_f(center[0], "center x"), _f(center[1], "center y")]
    m = [[_f(cov[i][j], "covariance") for j in range(2)] for i in range(2)]
    d2 = _f(geom.get("distance_square", 1.0), "distance_square")
    if d2 <= 0:
        raise GateError("ellipse distance_square must be > 0")
    if abs(m[0][1] - m[1][0]) > 1e-12 * max(1.0, abs(m[0][1])):
        raise GateError("ellipse covariance matrix must be symmetric")
    if m[0][0] <= 0 or m[1][1] <= 0 or m[0][0] * m[1][1] - m[0][1] * m[1][0] <= 0:
        raise GateError("ellipse covariance matrix must be positive definite")
    return {"center": c, "cov": m, "distance_square": d2}


def make_spec(kind: str, dims: list[Dim], geom: dict) -> GateSpec:
    if kind not in KINDS:
        raise GateError(f"unknown gate kind '{kind}'")
    if len(dims) != KINDS[kind]:
        raise GateError(f"{kind} gate needs {KINDS[kind]} dimension(s), got {len(dims)}")
    if len(dims) == 2 and dims[0].pnn == dims[1].pnn:
        raise GateError("a 2-D gate needs two different channels")
    norm = validate_geom(kind, geom)
    return GateSpec(kind, tuple(dims), json.dumps(norm, sort_keys=True))


def bbox(kind: str, geom: dict) -> tuple[float, float, float, float]:
    """(x0, x1, y0, y1) bounding box in scale units; +-inf where unbounded."""
    inf = math.inf
    if kind == "range":
        return (geom["x0"] if geom["x0"] is not None else -inf,
                geom["x1"] if geom["x1"] is not None else inf, -inf, inf)
    if kind == "rect":
        g = lambda k, d: geom[k] if geom[k] is not None else d  # noqa: E731
        return g("x0", -inf), g("x1", inf), g("y0", -inf), g("y1", inf)
    if kind == "polygon":
        v = np.asarray(geom["vertices"], dtype=np.float64)
        return float(v[:, 0].min()), float(v[:, 0].max()), float(v[:, 1].min()), float(v[:, 1].max())
    cov = np.asarray(geom["cov"], dtype=np.float64)
    d = math.sqrt(geom["distance_square"])
    hx, hy = d * math.sqrt(cov[0, 0]), d * math.sqrt(cov[1, 1])
    cx, cy = geom["center"]
    return cx - hx, cx + hx, cy - hy, cy + hy


def membership(kind: str, geom: dict, cols: list[np.ndarray], within: np.ndarray | None = None) -> np.ndarray:
    """Boolean membership for every event. `within` restricts evaluation to a parent mask."""
    x = cols[0]
    n = x.shape[0]
    if kind in ("rect", "range"):
        m = np.ones(n, dtype=bool) if within is None else within.copy()
        for ax, col in zip(("x", "y"), cols):
            lo, hi = geom[f"{ax}0"], geom[f"{ax}1"]
            if lo is not None:
                m &= col >= lo
            if hi is not None:
                m &= col < hi
            if lo is None or hi is None:
                m &= ~np.isnan(col)
        return m

    y = cols[1]
    x0, x1, y0, y1 = bbox(kind, geom)
    # Cheap bounding-box prefilter, then the exact test on candidates only.
    cand = (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
    if within is not None:
        cand &= within
    idx = np.flatnonzero(cand)
    out = np.zeros(n, dtype=bool)
    if idx.size == 0:
        return out
    pts = np.ascontiguousarray(np.column_stack((x[idx], y[idx])), dtype=np.float64)
    if kind == "polygon":
        inside = _fu_gating.points_in_polygon(np.asarray(geom["vertices"], dtype=np.float64), pts)
    else:
        inside = _fu_gating.points_in_ellipsoid(
            np.asarray(geom["cov"], dtype=np.float64),
            np.asarray(geom["center"], dtype=np.float64),
            geom["distance_square"],
            pts,
        )
    out[idx] = np.asarray(inside, dtype=bool)
    return out


def outline(kind: str, geom: dict, window: tuple[float, float, float, float], n_ellipse: int = 96) -> list[list[float]]:
    """Closed outline in the gate's own (x, y) scale units, clipped to `window` when unbounded."""
    wx0, wx1, wy0, wy1 = window
    if kind == "range":
        x0 = geom["x0"] if geom["x0"] is not None else wx0
        x1 = geom["x1"] if geom["x1"] is not None else wx1
        return [[x0, wy0], [x1, wy0], [x1, wy1], [x0, wy1]]
    if kind == "rect":
        x0 = geom["x0"] if geom["x0"] is not None else wx0
        x1 = geom["x1"] if geom["x1"] is not None else wx1
        y0 = geom["y0"] if geom["y0"] is not None else wy0
        y1 = geom["y1"] if geom["y1"] is not None else wy1
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    if kind == "polygon":
        return [list(v) for v in geom["vertices"]]
    cov = np.asarray(geom["cov"], dtype=np.float64)
    vals, vecs = np.linalg.eigh(cov)
    theta = np.linspace(0, 2 * math.pi, n_ellipse, endpoint=False)
    unit = np.stack((np.cos(theta), np.sin(theta)))
    pts = (vecs @ (np.sqrt(vals * geom["distance_square"])[:, None] * unit)).T + np.asarray(geom["center"])
    return pts.tolist()
