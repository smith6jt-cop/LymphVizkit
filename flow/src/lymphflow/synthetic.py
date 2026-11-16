"""Synthetic FCS files with exactly known populations and a known spillover matrix.

No real cytometry data is committed to the repository. These files exist so that every
gating / compensation / statistics result can be checked against ground truth:

* each event belongs to exactly one population, with *bounded* jitter around the population's
  centre in true (compensated) space, so a gate drawn in the gap between clusters has an
  exactly known count that float32 rounding cannot change;
* observed = true @ S for a known spillover matrix S, written as $SPILLOVER;
* the TEXT segment carries operator / institution / patient-style keywords so the
  de-identification allow-list can be tested.

Marker names follow the lymphoid vocabulary of LymphVizkit (CD3e, CD4, CD8a, CD20).
"""
from __future__ import annotations

import json
from array import array
from pathlib import Path

import flowio
import numpy as np

FLUORO = ["B530-A", "YG585-A", "R670-A", "V450-A"]
MARKERS = ["CD3e", "CD4", "CD8a", "CD20"]
CHANNELS = ["FSC-A", "SSC-A", *FLUORO, "Time"]
STAINS = ["", "", *MARKERS, ""]

# Rows = fluorochrome, columns = detector (FCS convention): observed = true @ SPILL.
SPILL = np.array(
    [
        [1.00, 0.12, 0.00, 0.02],
        [0.08, 1.00, 0.05, 0.00],
        [0.00, 0.03, 1.00, 0.00],
        [0.04, 0.00, 0.01, 1.00],
    ]
)

NEG_CENTER, NEG_HALF_WIDTH = 100.0, 250.0        # negatives span [-150, 350]
POS_CENTER, POS_LO, POS_HI = 20000.0, 0.6, 1.6    # positives span [12000, 32000]
GATE_THRESHOLD = 3000.0                           # in the gap between 350 and 12000

# name -> (CD3e, CD4, CD8a, CD20) positivity
POPULATIONS = {
    "CD4 T cell": (1, 1, 0, 0),
    "CD8 T cell": (1, 0, 1, 0),
    "B cell": (0, 0, 0, 1),
    "Other": (0, 0, 0, 0),
}
CELL_SCATTER = (110000.0, 60000.0, 0.25)   # FSC centre, SSC centre, relative half-width
DEBRIS_SCATTER = (12000.0, 8000.0, 0.5)
SCATTER_GATE = {"x0": 50000.0, "x1": 200000.0, "y0": 20000.0, "y1": 120000.0}  # contains all cells, no debris


def spill_text(detectors: list[str] = FLUORO, matrix: np.ndarray = SPILL) -> str:
    return ",".join([str(len(detectors)), *detectors, *(repr(float(v)) for v in matrix.ravel())])


def make_events(counts: dict[str, int], n_debris: int, seed: int, spill: np.ndarray | None = SPILL):
    """Return (events float32 [n, 7], truth dict). Row order is shuffled deterministically."""
    rng = np.random.default_rng(seed)
    blocks, labels = [], []
    for name, positivity in POPULATIONS.items():
        n = int(counts.get(name, 0))
        if n == 0:
            continue
        true = np.empty((n, 4))
        for j, pos in enumerate(positivity):
            if pos:
                true[:, j] = POS_CENTER * rng.uniform(POS_LO, POS_HI, n)
            else:
                true[:, j] = NEG_CENTER + rng.uniform(-NEG_HALF_WIDTH, NEG_HALF_WIDTH, n)
        fsc = CELL_SCATTER[0] * (1 + rng.uniform(-CELL_SCATTER[2], CELL_SCATTER[2], n))
        ssc = CELL_SCATTER[1] * (1 + rng.uniform(-CELL_SCATTER[2], CELL_SCATTER[2], n))
        blocks.append(np.column_stack((fsc, ssc, true)))
        labels += [name] * n
    if n_debris:
        true = NEG_CENTER + rng.uniform(-NEG_HALF_WIDTH, NEG_HALF_WIDTH, (n_debris, 4))
        fsc = DEBRIS_SCATTER[0] * (1 + rng.uniform(-DEBRIS_SCATTER[2], DEBRIS_SCATTER[2], n_debris))
        ssc = DEBRIS_SCATTER[1] * (1 + rng.uniform(-DEBRIS_SCATTER[2], DEBRIS_SCATTER[2], n_debris))
        blocks.append(np.column_stack((fsc, ssc, true)))
        labels += ["Debris"] * n_debris

    data = np.vstack(blocks)
    order = rng.permutation(data.shape[0])
    data = data[order]
    labels = [labels[i] for i in order]
    true_fluoro = data[:, 2:6].copy()
    if spill is not None:
        data[:, 2:6] = true_fluoro @ spill
    time = np.arange(data.shape[0], dtype=np.float64)  # raw ticks; $TIMESTEP scales them
    events = np.column_stack((data, time)).astype(np.float32)

    truth = {
        "n_events": int(events.shape[0]),
        "counts": {**{k: int(v) for k, v in counts.items() if v}, "Debris": int(n_debris)},
        "n_cells": int(sum(counts.values())),
        "labels": labels,
        "true_fluoro_median": {
            name: {m: float(np.median(true_fluoro[[lab == name for lab in labels], j])) for j, m in enumerate(MARKERS)}
            for name in {*labels}
        },
    }
    return events, truth


def write_fcs(path: Path, events: np.ndarray, *, spill: str | None = "spillover",
              spill_matrix: np.ndarray = SPILL, identifying: bool = True,
              channels: list[str] = CHANNELS, stains: list[str] = STAINS) -> None:
    """Write events as FCS 3.1 float. `spill`: 'spillover' ($SPILLOVER), 'spill' (vendor SPILL) or None."""
    meta: dict[str, str] = {"timestep": "0.01", "cyt": "SyntheticCytometer"}
    if spill:
        meta[spill] = spill_text(matrix=spill_matrix)
    if identifying:
        meta.update(
            {
                "op": "Jane Operator",
                "inst": "Example University Flow Core",
                "src": "Patient 0042 spleen",
                "smno": "MRN-0042",
                "fil": "Patient_0042_spleen.fcs",
                "date": "07-JUL-2026",
                "btim": "10:15:00",
                "etim": "10:17:30",
                "cytsn": "SN123456",
                "case number": "0042",
                "donor age": "57",
            }
        )
    flat = array("f")
    flat.frombytes(np.ascontiguousarray(events, dtype="<f4").tobytes())
    with open(path, "wb") as fh:
        flowio.create_fcs(fh, flat, channels, opt_channel_names=stains, metadata_dict=meta)


def make_dataset(out_dir: Path, n_files: int = 6, events_per_file: int = 20000, seed: int = 0) -> dict:
    """Write `n_files` FCS files in two groups with different compositions, plus truth.json."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    groups = ["Resting", "Reactive"]
    base = {"Resting": (0.30, 0.15, 0.35, 0.20), "Reactive": (0.22, 0.28, 0.40, 0.10)}
    manifest: dict = {"channels": CHANNELS, "markers": dict(zip(FLUORO, MARKERS)), "spill": SPILL.tolist(),
                      "gate_threshold": GATE_THRESHOLD, "scatter_gate": SCATTER_GATE, "files": {}}
    for i in range(n_files):
        group = groups[i % 2]
        n_debris = int(events_per_file * 0.1)
        n_cells = events_per_file - n_debris
        fracs = np.array(base[group]) + rng.uniform(-0.02, 0.02, 4)
        fracs /= fracs.sum()
        raw_counts = np.floor(fracs * n_cells).astype(int)
        raw_counts[-1] += n_cells - raw_counts.sum()
        counts = dict(zip(POPULATIONS, (int(c) for c in raw_counts)))
        events, truth = make_events(counts, n_debris, seed=seed * 1000 + i)
        name = f"synthetic_{group.lower()}_{i + 1:02d}.fcs"
        write_fcs(out_dir / name, events)
        truth.pop("labels")
        manifest["files"][name] = {"group": group, **truth}
    (out_dir / "truth.json").write_text(json.dumps(manifest, indent=2))
    return manifest
