"""Scale (transform) model.

A Scale is immutable and content-addressed. Gate dimensions reference a Scale, never "the
channel's current display scale", so editing how a channel is displayed cannot move a gate.

All five GatingML 2.0 parametrised scales are supported and computed by the same FlowUtils C
routines FlowKit calls, so transformed values are bit-identical to FlowKit's:

  linear    identity (no parameters) or GatingML flin(T, A) = (x + A) / (T + A)
  log       GatingML flog(T, M)
  asinh     GatingML fasinh(T, M, A); the UI's "cofactor c" form is T, M = asinh(T/c)/ln 10, A = 0
  logicle   GatingML logicle(T, W, M, A)
  hyperlog  GatingML hyperlog(T, W, M, A)

Parameters are validated *before* the C routine is called: FlowUtils does not validate them
and returns -1 for values on which the solver fails to converge instead of raising.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

import numpy as np
from flowutils import transforms as _fu

KINDS = ("linear", "log", "asinh", "logicle", "hyperlog")
_SUPERSCRIPT = str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹")


class ScaleError(ValueError):
    pass


@dataclass(frozen=True)
class Scale:
    kind: str
    params: tuple[tuple[str, float], ...] = ()  # sorted (name, value) pairs

    @property
    def p(self) -> dict[str, float]:
        return dict(self.params)

    @property
    def hash(self) -> str:
        payload = json.dumps({"kind": self.kind, "params": [[k, repr(v)] for k, v in self.params]})
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def is_identity(self) -> bool:
        return self.kind == "linear" and not self.params

    def to_json(self) -> dict:
        return {"kind": self.kind, "params": self.p, "hash": self.hash, "describe": describe(self)}


def _num(params: dict, name: str) -> float:
    try:
        v = float(params[name])
    except KeyError:
        raise ScaleError(f"missing scale parameter '{name}'") from None
    except (TypeError, ValueError):
        raise ScaleError(f"scale parameter '{name}' is not a number") from None
    if not math.isfinite(v):
        raise ScaleError(f"scale parameter '{name}' is not finite")
    return v


def make_scale(kind: str, params: dict | None = None) -> Scale:
    """Validate and normalise. Raises ScaleError with a specific message."""
    params = dict(params or {})
    if kind not in KINDS:
        raise ScaleError(f"unknown scale kind '{kind}' (expected one of {', '.join(KINDS)})")

    if kind == "linear":
        if "t" not in params and "a" not in params:
            return Scale("linear")
        t, a = _num(params, "t"), float(params.get("a", 0.0))
        if t <= 0:
            raise ScaleError("linear: T must be > 0")
        if not (0 <= a <= t):
            raise ScaleError("linear: A must satisfy 0 <= A <= T")
        out = {"t": t, "a": a}

    elif kind == "log":
        t, m = _num(params, "t"), _num(params, "m")
        if t <= 0 or m <= 0:
            raise ScaleError("log: T and M must be > 0")
        out = {"t": t, "m": m}

    elif kind == "asinh":
        t = _num(params, "t")
        if t <= 0:
            raise ScaleError("asinh: T must be > 0")
        if "cofactor" in params and "m" not in params:
            c = _num(params, "cofactor")
            if c <= 0:
                raise ScaleError("asinh: cofactor must be > 0")
            m = math.asinh(t / c) / math.log(10)
        else:
            m = _num(params, "m")
        a = float(params.get("a", 0.0))
        if m <= 0:
            raise ScaleError("asinh: M must be > 0")
        if not (0 <= a <= m):
            raise ScaleError("asinh: A must satisfy 0 <= A <= M")
        out = {"t": t, "m": m, "a": a}

    else:  # logicle, hyperlog
        t, w, m = _num(params, "t"), _num(params, "w"), _num(params, "m")
        a = float(params.get("a", 0.0))
        if t <= 0:
            raise ScaleError(f"{kind}: T must be > 0")
        if m <= 0:
            raise ScaleError(f"{kind}: M must be > 0")
        if kind == "hyperlog" and w <= 0:
            raise ScaleError("hyperlog: W must be > 0")
        if w < 0 or 2 * w > m:
            raise ScaleError(f"{kind}: W must satisfy 0 <= W <= M/2")
        if not (-w <= a <= m - 2 * w):
            raise ScaleError(f"{kind}: A must satisfy -W <= A <= M - 2W")
        out = {"t": t, "w": w, "m": m, "a": a}

    scale = Scale(kind, tuple(sorted((k, float(v)) for k, v in out.items())))
    _probe(scale)
    return scale


def _probe(scale: Scale) -> None:
    """Reject parameter sets on which the numerical routine misbehaves."""
    p = scale.p
    t = p.get("t", 1.0)
    lo = 1e-6 * t if scale.kind == "log" else -0.05 * t
    x = np.linspace(lo, t, 257)
    with np.errstate(all="ignore"):
        y = _forward_finite(scale, x)
        back = _inverse_finite(scale, y)
    if not np.all(np.isfinite(y)) or np.any(np.diff(y) < -1e-12):
        raise ScaleError(f"{scale.kind}: these parameters do not give a monotonic scale")
    if not np.allclose(back, x, rtol=1e-6, atol=1e-6 * t):
        raise ScaleError(f"{scale.kind}: these parameters do not invert reliably (solver did not converge)")


def _forward_finite(scale: Scale, x: np.ndarray) -> np.ndarray:
    p = scale.p
    k = scale.kind
    if k == "linear":
        return x.copy() if scale.is_identity else (x + p["a"]) / (p["t"] + p["a"])
    if k == "log":
        return _fu.log(x, None, t=p["t"], m=p["m"])
    if k == "asinh":
        return _fu.asinh(x, None, t=p["t"], m=p["m"], a=p["a"])
    if k == "logicle":
        return _fu.logicle(x, None, t=p["t"], m=p["m"], w=p["w"], a=p["a"])
    return _fu.hyperlog(x, None, t=p["t"], m=p["m"], w=p["w"], a=p["a"])


def _inverse_finite(scale: Scale, y: np.ndarray) -> np.ndarray:
    p = scale.p
    k = scale.kind
    if k == "linear":
        return y.copy() if scale.is_identity else y * (p["t"] + p["a"]) - p["a"]
    if k == "log":
        return _fu.log_inverse(y, None, t=p["t"], m=p["m"])
    if k == "asinh":
        return _fu.asinh_inverse(y, None, t=p["t"], m=p["m"], a=p["a"])
    if k == "logicle":
        return _fu.logicle_inverse(y, None, t=p["t"], m=p["m"], w=p["w"], a=p["a"])
    return _fu.hyperlog_inverse(y, None, t=p["t"], m=p["m"], w=p["w"], a=p["a"])


def forward(scale: Scale, x: np.ndarray) -> np.ndarray:
    """Raw (linear) values -> scale units, float64. Non-finite inputs stay NaN."""
    x = np.asarray(x, dtype=np.float64)
    if scale.is_identity:
        return x
    finite = np.isfinite(x)
    if finite.all():
        with np.errstate(all="ignore"):
            return np.asarray(_forward_finite(scale, np.ascontiguousarray(x)), dtype=np.float64)
    out = np.full(x.shape, np.nan)
    with np.errstate(all="ignore"):
        out[finite] = _forward_finite(scale, np.ascontiguousarray(x[finite]))
    return out


def inverse(scale: Scale, y: np.ndarray) -> np.ndarray:
    y = np.asarray(y, dtype=np.float64)
    if scale.is_identity:
        return y
    finite = np.isfinite(y)
    out = np.full(y.shape, np.nan)
    with np.errstate(all="ignore"):
        out[finite] = _inverse_finite(scale, np.ascontiguousarray(y[finite]))
    return out


def describe(scale: Scale) -> str:
    """Parameters in force, shown on the axis so a plot is never ambiguous about its scale."""
    p = scale.p
    f = lambda v: f"{v:g}"  # noqa: E731
    if scale.kind == "linear":
        return "linear" if scale.is_identity else f"linear T={f(p['t'])} A={f(p['a'])}"
    if scale.kind == "log":
        return f"log T={f(p['t'])} M={f(p['m'])}"
    if scale.kind == "asinh":
        cofactor = p["t"] / math.sinh(p["m"] * math.log(10))
        extra = f" A={f(p['a'])}" if p["a"] else ""
        return f"arcsinh cofactor={cofactor:.4g} T={f(p['t'])}{extra}"
    return f"{scale.kind} T={f(p['t'])} W={f(p['w'])} M={f(p['m'])} A={f(p['a'])}"


# ---- defaults ---------------------------------------------------------------

def default_scale(channel_kind: str, pnr: float | None, vmax: float | None = None) -> Scale:
    """Scatter and time are linear; fluorescence is logicle with T from $PnR."""
    if channel_kind in ("scatter", "time"):
        return Scale("linear")
    t = pnr if pnr and math.isfinite(pnr) and pnr > 1 else None
    if t is None:
        t = float(vmax) if vmax and math.isfinite(vmax) and vmax > 1 else 262144.0
    return make_scale("logicle", {"t": float(t), "w": 0.5, "m": 4.5, "a": 0.0})


def default_window(scale: Scale, channel_kind: str, pnr: float | None,
                   vmin: float | None, vmax: float | None) -> tuple[float, float]:
    """Display window in scale units. Events outside it pile up in the edge bins."""
    if scale.is_identity:
        lo = 0.0 if channel_kind == "scatter" else (vmin if vmin is not None else 0.0)
        hi = max(v for v in (pnr, vmax, lo + 1.0) if v is not None and math.isfinite(v))
        if channel_kind == "time" and vmax is not None:
            hi = vmax
        if vmin is not None and vmin < lo:
            lo = vmin
        return float(lo), float(hi)
    if scale.kind == "asinh" and scale.p.get("a", 0.0) == 0.0:
        return -0.15, 1.02
    if scale.kind == "log":
        return 0.0, 1.02
    return -0.02, 1.02


# ---- ticks ------------------------------------------------------------------

def _pow10_label(sign: int, k: int) -> str:
    return ("-" if sign < 0 else "") + "10" + str(k).translate(_SUPERSCRIPT)


def _si(v: float) -> str:
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:g}M"
    if a >= 1e3:
        return f"{v / 1e3:g}K"
    return f"{v:g}"


def ticks(scale: Scale, lo: float, hi: float) -> list[dict]:
    """Tick positions in scale units, labelled in raw units."""
    if hi <= lo:
        return []
    if scale.is_identity:
        span = hi - lo
        step = 10 ** math.floor(math.log10(span / 5))
        for mult in (1, 2, 5, 10):
            if span / (step * mult) <= 6:
                step *= mult
                break
        first = math.ceil(lo / step) * step
        out, v = [], first
        while v <= hi + 1e-9 * span:
            out.append({"pos": float(v), "label": _si(round(v, 10)), "major": True})
            v += step
        return out

    t = scale.p.get("t", 262144.0)
    top = int(math.ceil(math.log10(t))) if t > 0 else 6
    raw: list[tuple[float, str, bool]] = [] if scale.kind == "log" else [(0.0, "0", True)]
    for k in range(0, top + 1):
        for sign in ((1,) if scale.kind == "log" else (1, -1)):
            raw.append((sign * 10.0**k, _pow10_label(sign, k), True))
            for mult in range(2, 10):
                raw.append((sign * mult * 10.0**k, "", False))
    values = np.array([r[0] for r in raw], dtype=np.float64)
    pos = forward(scale, values)
    out = []
    for (value, label, major), p in zip(raw, pos):
        if math.isfinite(p) and lo <= p <= hi:
            out.append({"pos": float(p), "label": label, "major": major, "raw": value})
    out.sort(key=lambda d: d["pos"])
    return out
