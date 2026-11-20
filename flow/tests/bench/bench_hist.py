#!/usr/bin/env python3
"""Latency benchmark of the interactive path on a 2-core budget.

    taskset -c 0,1 python tests/bench/bench_hist.py --sizes 100000 1000000 5000000 --fluoro 28

Each size is a synthetic file of 2 scatter + N fluorescence + 1 time parameter with a dense
N x N spillover matrix, ingested through the normal FCS -> Parquet path. Measured:

  cold  first density plot of a fluorescence pair (Parquet read of every detector column,
        compensation solve, two logicle transforms, binning)
  warm  repeated density plot, all events and under a parent gate (cached columns)
  gate  first evaluation of a polygon gate under a parent; statistics for all channels

Targets (warm, 95th percentile): < 60 ms at 1e6 events, < 250 ms at 5e6 events.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import tempfile
import time
from array import array
from pathlib import Path

for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(var, "2")

import flowio  # noqa: E402
import numpy as np  # noqa: E402

from lymphflow.core import fcs_ingest, gates, hist, scales  # noqa: E402
from lymphflow.core import stats as stats_mod  # noqa: E402
from lymphflow.core.cache import ArrayCache  # noqa: E402
from lymphflow.core.engine import ChannelInfo, Engine, GateNode, SampleData, make_dim  # noqa: E402


def make_fcs(path: Path, n: int, n_fluoro: int, seed: int = 0) -> None:
    rng = np.random.default_rng(seed)
    scatter = rng.normal([110000, 60000], [20000, 12000], (n, 2))
    positive = rng.random((n, n_fluoro)) < 0.3
    true = np.where(positive, rng.lognormal(9.5, 0.5, (n, n_fluoro)), rng.normal(100, 120, (n, n_fluoro)))
    spill = np.eye(n_fluoro) + np.triu(rng.uniform(0, 0.08, (n_fluoro, n_fluoro)), 1) \
        + np.tril(rng.uniform(0, 0.03, (n_fluoro, n_fluoro)), -1)
    data = np.column_stack((scatter, true @ spill, np.arange(n))).astype("<f4")
    names = ["FSC-A", "SSC-A", *[f"FL{i + 1}-A" for i in range(n_fluoro)], "Time"]
    text = ",".join([str(n_fluoro), *names[2:-1], *(repr(float(v)) for v in spill.ravel())])
    flat = array("f")
    flat.frombytes(data.tobytes())
    del data, true, scatter
    with open(path, "wb") as fh:
        flowio.create_fcs(fh, flat, names, metadata_dict={"spillover": text})


def timed(fn, reps: int = 1) -> list[float]:
    out = []
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        out.append(1000 * (time.perf_counter() - t))
    return out


def pct(values: list[float], q: float) -> float:
    return float(np.percentile(values, q))


def bench(n: int, n_fluoro: int, work: Path, reps: int) -> dict:
    fcs, parquet = work / f"bench_{n}.fcs", work / f"bench_{n}.parquet"
    make_fcs(fcs, n, n_fluoro)
    t = time.perf_counter()
    r = fcs_ingest.ingest_fcs(fcs, parquet)
    ingest_s = time.perf_counter() - t
    fcs_mb, parquet_mb = fcs.stat().st_size / 1e6, parquet.stat().st_size / 1e6
    fcs.unlink()

    cache = ArrayCache(8 * 1024**3)
    engine = Engine(cache, work / f"masks_{n}")
    s = SampleData(key=r.sha256, name=f"bench_{n}", n_events=r.n_events,
                   channels=tuple(ChannelInfo(c.idx, c.col, c.pnn, c.pns, c.kind, c.pnr, c.vmin, c.vmax) for c in r.channels),
                   loader=fcs_ingest.parquet_loader(parquet), cache=cache, comp=r.comp, comp_state=r.comp_state)
    lin, logicle = scales.make_scale("linear"), scales.default_scale("fluoro", 262144.0)
    win = (-0.02, 1.02)

    def density(x: str, y: str, node=None):
        mask = engine.mask(s, node)
        counts, *_ = hist.hist2d(s.scaled(x, logicle), s.scaled(y, logicle), mask, win, win, 256)
        return hist.pack_frame({"nb": 256}, counts)

    cells = GateNode(1, "Cells", gates.make_spec("rect", [make_dim("FSC-A", lin), make_dim("SSC-A", lin)],
                                                 {"x0": 70000, "x1": 150000, "y0": 36000, "y1": 84000}))
    poly = GateNode(2, "P", gates.make_spec("polygon", [make_dim("FL1-A", logicle), make_dim("FL2-A", logicle)],
                                            {"vertices": [[0.55, -1], [2, -1], [2, 0.5], [0.8, 0.62], [0.55, 0.5]]}), cells)

    cold = timed(lambda: density("FL1-A", "FL2-A"))[0]
    new_pair = timed(lambda: density("FL3-A", "FL4-A"))[0]            # comp block cached, two new transforms
    warm_all = timed(lambda: density("FL1-A", "FL2-A"), reps)
    scatter_gate = timed(lambda: engine.mask(s, cells))[0]
    polygon_gate = timed(lambda: engine.mask(s, poly))[0]
    warm_gated = timed(lambda: density("FL1-A", "FL2-A", poly), reps)
    stats_ms = timed(lambda: stats_mod.population_stats(engine, s, poly))[0]
    return {
        "events": n, "parameters": r.n_params, "fcs_mb": round(fcs_mb, 1), "parquet_mb": round(parquet_mb, 1),
        "ingest_s": round(ingest_s, 2),
        "cold_first_plot_ms": round(cold, 1), "new_channel_pair_ms": round(new_pair, 1),
        "warm_all_events_p50_ms": round(pct(warm_all, 50), 1), "warm_all_events_p95_ms": round(pct(warm_all, 95), 1),
        "warm_gated_p50_ms": round(pct(warm_gated, 50), 1), "warm_gated_p95_ms": round(pct(warm_gated, 95), 1),
        "rect_gate_first_ms": round(scatter_gate, 1), "polygon_gate_first_ms": round(polygon_gate, 1),
        "stats_all_channels_ms": round(stats_ms, 1),
        "gated_events": int(engine.count(s, poly)), "cache_mb": round(cache.nbytes / 1e6),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", type=int, nargs="+", default=[100_000, 1_000_000, 5_000_000])
    ap.add_argument("--fluoro", type=int, default=28)
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--out", default=str(Path(__file__).parent / "results" / "bench_hist.json"))
    args = ap.parse_args()
    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    results = []
    with tempfile.TemporaryDirectory(prefix="lymphflow-bench-") as tmp:
        for n in args.sizes:
            res = bench(n, args.fluoro, Path(tmp), args.reps)
            results.append(res)
            print(json.dumps(res), flush=True)
    report = {"cpu_affinity": affinity, "blas_threads": os.environ.get("OMP_NUM_THREADS"),
              "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
              "numpy": np.__version__, "python": sys.version.split()[0], "results": results}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))
    targets = {1_000_000: 60.0, 5_000_000: 250.0}
    ok = all(r["warm_gated_p95_ms"] < targets[r["events"]] and r["warm_all_events_p95_ms"] < targets[r["events"]]
             for r in results if r["events"] in targets)
    print(f"affinity {affinity}; peak RSS {report['peak_rss_mb']} MB; warm p95 targets {'MET' if ok else 'NOT MET'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
