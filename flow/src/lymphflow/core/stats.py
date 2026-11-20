"""Population statistics.

Always computed on linear values (compensated where compensation applies) in float64 -
never on display-scaled values, whatever scale the plot happens to use.

  median, mean, sd   over finite values in the population
  cv                 100 * sd / mean                      (None when mean == 0)
  rcv                100 * (p84.13 - p15.87) / (2 * median)   robust CV (None when median == 0)
  geomean            geometric mean of the strictly positive values only;
                     `n_nonpositive` reports how many values that excluded
"""
from __future__ import annotations

import math

import numpy as np

from .engine import Engine, GateNode, SampleData


def _clean(v: float) -> float | None:
    return float(v) if math.isfinite(v) else None


def channel_stats(values: np.ndarray) -> dict:
    v = values[np.isfinite(values)]
    n = int(v.shape[0])
    out = {"n": n, "n_nonfinite": int(values.shape[0] - n), "median": None, "mean": None, "sd": None,
           "cv": None, "rcv": None, "geomean": None, "n_nonpositive": 0}
    if n == 0:
        return out
    p16, med, p84 = np.percentile(v, [15.87, 50.0, 84.13])
    mean = float(v.mean())
    sd = float(v.std(ddof=1)) if n > 1 else 0.0
    pos = v[v > 0]
    out.update(
        median=_clean(med),
        mean=_clean(mean),
        sd=_clean(sd),
        cv=_clean(100.0 * sd / mean) if mean != 0 else None,
        rcv=_clean(100.0 * (p84 - p16) / (2.0 * med)) if med != 0 else None,
        geomean=_clean(math.exp(float(np.log(pos).mean()))) if pos.shape[0] else None,
        n_nonpositive=int(n - pos.shape[0]),
    )
    return out


def population_stats(engine: Engine, sample: SampleData, node: GateNode | None,
                     channels: list[str] | None = None) -> dict:
    """Counts plus per-channel statistics for one population of one sample."""
    mask = engine.mask(sample, node)
    count = sample.n_events if mask is None else int(np.count_nonzero(mask))
    parent_count = sample.n_events if node is None or node.parent is None else engine.count(sample, node.parent)
    if channels is None:
        channels = [c.pnn for c in sample.channels if c.kind != "time"]
    per_channel = {}
    for pnn in channels:
        values = sample.linear(pnn, "sample")
        per_channel[pnn] = channel_stats(values if mask is None else values[mask])
    return {
        "count": count,
        "parent_count": parent_count,
        "total": sample.n_events,
        "pct_parent": (100.0 * count / parent_count) if parent_count else None,
        "pct_total": (100.0 * count / sample.n_events) if sample.n_events else None,
        "channels": per_channel,
        "gate_key": engine.gate_key(sample, node),
        "comp_state": sample.comp_state,
        "comp_applied": sample.comp_active,
        "comp_hash": sample.comp_hash,
        "overridden": _any_override(node),
        "values": "compensated linear" if sample.comp_active else "linear (no compensation applied)",
    }


def stats_key(engine: Engine, sample: SampleData, node: GateNode | None) -> str:
    """Identity of a statistics row: the gate chain plus the compensation the values used."""
    return engine.gate_key(sample, node) + "|" + sample.comp_hash


def _any_override(node: GateNode | None) -> bool:
    while node is not None:
        if node.overridden:
            return True
        node = node.parent
    return False
