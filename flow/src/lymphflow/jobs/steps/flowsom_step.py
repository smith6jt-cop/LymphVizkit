"""FlowSOM clustering of one population across all samples.

Design points that guard against the usual clustering artefacts:

* Training uses an *equal, seeded* subsample from every sample (never "the first N events",
  never proportional to file size), and the subsample indices are written to out/.
* Cluster frequencies are NOT taken from the subsample. After training, every event of the
  population in every sample is mapped to its nearest SOM node, so frequencies are over all
  gated events.
* The input is each channel's scale-transformed value under the scale recorded in the job
  parameters (compensated where compensation applies). Heatmap medians are in those scale
  units and are labelled as such; they are not linear MFIs.

`flowsom` (GPL-3) is imported only here, inside the job process.
"""
from __future__ import annotations

import numpy as np

from ... import snapshot as snap
from ...core.engine import CompensationInvalid, PanelMismatch

BATCH = 500_000


def _matrix(sdata, channels: list[dict], rows: np.ndarray | None) -> np.ndarray:
    cols = []
    for ch in channels:
        v = sdata.scaled(ch["pnn"], snap.scale_from_dict(ch["scale"]), "sample")
        cols.append(v if rows is None else v[rows])
    return np.column_stack(cols).astype(np.float32)


def run(ctx) -> dict:
    from flowsom.models import FlowSOMEstimator  # GPL-3; job process only

    p = ctx.params
    gate_id = int(p.get("gate_id") or 0)
    channels: list[dict] = p["channels"]            # [{pnn, scale: {kind, params}}]
    if len(channels) < 2:
        raise ValueError("FlowSOM needs at least two channels")
    n_per_sample = int(p.get("n_per_sample", 20000))
    xdim, ydim = int(p.get("xdim", 10)), int(p.get("ydim", 10))
    n_meta = int(p.get("n_clusters", 10))
    seed = int(p.get("seed", 42))
    pnns = [c["pnn"] for c in channels]

    # ---- 1. equal seeded subsample per sample ------------------------------------------
    usable, skipped, train_blocks = [], [], []
    n_samples = len(ctx.snapshot["samples"])
    for i, (d, sdata, nodes) in enumerate(ctx.samples()):
        ctx.progress(0.4 * i / max(n_samples, 1), f"sampling {d['name']}")
        try:
            sdata.require_usable_comp()
            sdata.require(pnns)
            node = nodes[gate_id] if gate_id else None
            mask = ctx.engine.mask(sdata, node)
        except (PanelMismatch, CompensationInvalid) as exc:
            skipped.append({"sample_id": d["id"], "sample": d["name"], "error": str(exc)})
            continue
        idx = np.arange(sdata.n_events) if mask is None else np.flatnonzero(mask)
        block = _matrix(sdata, channels, idx)
        finite = np.isfinite(block).all(axis=1)
        idx, block = idx[finite], block[finite]
        if idx.size == 0:
            skipped.append({"sample_id": d["id"], "sample": d["name"], "error": "population is empty"})
            continue
        rng = np.random.default_rng([seed, int(d["id"])])
        take = np.sort(rng.choice(idx.size, size=min(n_per_sample, idx.size), replace=False))
        np.save(ctx.out_dir / f"subsample_{d['id']}.npy", idx[take])
        train_blocks.append(block[take])
        usable.append({"id": d["id"], "name": d["name"], "group": d.get("group_label", ""),
                       "n_population": int(idx.size), "n_train": int(take.size)})
        sdata.release()
    if not usable:
        raise ValueError("no sample could be used: " + "; ".join(s["error"] for s in skipped))

    # ---- 2. train ----------------------------------------------------------------------
    ctx.progress(0.45, "training FlowSOM")
    train = np.vstack(train_blocks)
    del train_blocks
    n_meta = max(2, min(n_meta, xdim * ydim - 1))
    est = FlowSOMEstimator(xdim=xdim, ydim=ydim, seed=seed, n_clusters=n_meta)
    train_meta = np.asarray(est.fit_predict(train))
    node_to_meta = np.asarray(est._y_codes)
    # Largest metacluster first. A metacluster made only of SOM nodes that attracted no
    # training event is kept (events can still map to it) and has no median.
    train_counts = {int(m): int(np.count_nonzero(train_meta == m)) for m in np.unique(node_to_meta)}
    labels = sorted(train_counts, key=lambda m: (-train_counts[m], m))

    # Heatmap: median scale-unit value per metacluster, over the training subsample.
    medians = [[float(np.median(train[train_meta == m, j])) if train_counts[m] else None
                for j in range(len(channels))] for m in labels]

    # ---- 3. map ALL events of the population to SOM nodes --------------------------------
    counts = {}
    by_id = {s["id"]: s for s in usable}
    done = 0
    for d, sdata, nodes in ctx.samples():
        if d["id"] not in by_id:
            continue
        ctx.progress(0.5 + 0.5 * done / len(usable), f"mapping {d['name']}")
        node = nodes[gate_id] if gate_id else None
        mask = ctx.engine.mask(sdata, node)
        idx = np.arange(sdata.n_events) if mask is None else np.flatnonzero(mask)
        per_meta = np.zeros(len(labels), dtype=np.int64)
        n_mapped = 0
        for start in range(0, idx.size, BATCH):
            block = _matrix(sdata, channels, idx[start:start + BATCH])
            block = block[np.isfinite(block).all(axis=1)]
            if block.shape[0] == 0:
                continue
            meta = node_to_meta[np.asarray(est.cluster_model.predict(block))]
            for k, m in enumerate(labels):
                per_meta[k] += int(np.count_nonzero(meta == m))
            n_mapped += block.shape[0]
        counts[str(d["id"])] = {"counts": per_meta.tolist(), "n_mapped": int(n_mapped),
                                "pct": (100.0 * per_meta / n_mapped).tolist() if n_mapped else None}
        sdata.release()
        done += 1

    # Metaclusters made only of SOM nodes that no event maps to (neither in training nor over
    # all events) carry no information; drop them from the report and say how many there were.
    totals = np.zeros(len(labels), dtype=np.int64)
    for c in counts.values():
        totals += np.asarray(c["counts"], dtype=np.int64)
    keep = [i for i, m in enumerate(labels) if train_counts[m] > 0 or totals[i] > 0]
    n_empty = len(labels) - len(keep)
    labels = [labels[i] for i in keep]
    medians = [medians[i] for i in keep]
    for c in counts.values():
        c["counts"] = [c["counts"][i] for i in keep]
        c["pct"] = [c["pct"][i] for i in keep] if c["pct"] is not None else None

    import flowsom

    return {
        "type": "flowsom",
        "gate_id": gate_id,
        "channels": [{"pnn": c["pnn"], "pns": c.get("pns", ""), "scale": c["scale"]} for c in channels],
        "params": {"n_per_sample": n_per_sample, "xdim": xdim, "ydim": ydim, "n_clusters": n_meta, "seed": seed},
        "flowsom_version": getattr(flowsom, "__version__", "unknown"),
        "clusters": [f"MC{i + 1}" for i in range(len(labels))],
        "train_counts": [train_counts[m] for m in labels],
        "n_empty_metaclusters": n_empty,
        "medians": medians,
        "medians_note": "median of scale-transformed values over the training subsample (not linear MFI)",
        "samples": usable,
        "skipped": skipped,
        "frequencies": counts,
        "frequencies_note": "all events of the population mapped to SOM nodes; not subsample frequencies",
        "n_train": int(train.shape[0]),
    }
