#!/usr/bin/env python3
"""Generate a tiny SYNTHETIC spleen/lymph-node follicle dataset for LymphVizkit.

The real (large) dataset is not committed. This script writes a small, schema-correct
example under ``data/app_data/`` so the Shiny app BOOTS and every data tab renders via
the Excel-fallback path (no h5ad / reticulate / DuckDB required).

Outputs (all under data/app_data/):
  master_results.xlsx        4 sheets: Follicle_Markers / Follicle_Targets /
                             Follicle_Composition / LGALS3
  cells/{case}_Follicle_{n}.csv   per-follicle single-cell tables (drill-down)
  follicle_spatial_lookup.csv     follicle centroids
  json/{case}.geojson             QuPath-style segmentation boundaries (pixels)
  phenotype_rules.csv             marker -> phenotype gating rules (lymphoid)

The domain defaults here MIRROR app/shiny_app/R/00_domain_config.R (DOMAIN). Keep the
two in sync: physical schema tokens ("Donor Status" column; follicle_core/band/union
region types; Ins_/Glu_/Stt_ composition columns) are join keys the loader expects.
See docs/data_contract.md.

Usage:
  python scripts/make_synthetic_follicle_data.py [--out data/app_data] [--seed 7]
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Domain defaults (mirror app/shiny_app/R/00_domain_config.R)
# ---------------------------------------------------------------------------
PIXEL_SIZE_UM = 0.5078
EXPANSION_UM = 20.0

# Grouping axis: physical column stays "Donor Status"; values are the config levels.
GROUP_LEVELS = ["Resting", "Reactive", "Involuted"]

# Donors: 4 synthetic cases spread across >= 2 groups.
DONORS = [
    {"case": "0101", "status": "Resting",   "age": 34, "sex": "F"},
    {"case": "0102", "status": "Resting",   "age": 41, "sex": "M"},
    {"case": "0103", "status": "Reactive",  "age": 52, "sex": "F"},
    {"case": "0104", "status": "Involuted", "age": 67, "sex": "M"},
]
N_FOLLICLES = 6  # per donor

# Follicle-defining markers -> composition-sheet physical columns.
# (single_col/any_col are the historical Ins/Glu/Stt tokens the loader reads.)
DEFINING = [
    {"marker": "CD20", "single": "Ins_single", "any": "Ins_any"},
    {"marker": "BCL6", "single": "Glu_single", "any": "Glu_any"},
    {"marker": "CD21", "single": "Stt_single", "any": "Stt_any"},
]

# Full lymphoid marker panel (DAPI excluded from selectors upstream).
MARKERS = ["CD20", "CD21", "CD23", "CD35", "BCL6", "Ki67", "CD3e", "CD4", "CD8a",
           "FOXP3", "PD1", "CXCR5", "CD68", "CD163", "CD11c", "HLADR", "CD138",
           "CD56", "MPO", "CD31", "CD34", "PDPN", "LYVE1", "SMA", "Vimentin"]

PHENOTYPES = ["GC B cell", "Mantle B cell", "B cell", "Plasma cell", "FDC",
              "Tfh cell", "CD4 T cell", "CD8 T cell", "Treg", "Macrophage",
              "Tingible-body macrophage", "Dendritic cell", "NK cell",
              "Neutrophil", "Endothelial", "Lymphatic", "Stromal"]

# Marker most associated with each phenotype (to make expression non-degenerate).
PHENO_MARKER = {
    "GC B cell": "BCL6", "Mantle B cell": "CD20", "B cell": "CD20",
    "Plasma cell": "CD138", "FDC": "CD21", "Tfh cell": "PD1",
    "CD4 T cell": "CD4", "CD8 T cell": "CD8a", "Treg": "FOXP3",
    "Macrophage": "CD68", "Tingible-body macrophage": "CD163",
    "Dendritic cell": "CD11c", "NK cell": "CD56", "Neutrophil": "MPO",
    "Endothelial": "CD31", "Lymphatic": "PDPN", "Stromal": "Vimentin",
}


# ---------------------------------------------------------------------------
# phenotype_rules.csv (parser: row1 = [_, _, markers...]; rows = parent,pheno,cells)
# ---------------------------------------------------------------------------
RULE_MARKERS = ["CD20", "BCL6", "Ki67", "CD21", "CD35", "CD138", "CD3e", "CD4",
                "CD8a", "FOXP3", "PD1", "CD68", "CD163", "CD11c", "HLADR", "CD56",
                "MPO", "CD31", "CD34", "PDPN", "SMA", "Vimentin"]
# (parent, phenotype, {marker: pos|anypos|allneg})
RULES = [
    ("Immune",     "B cell",                   {"CD20": "pos", "CD3e": "allneg"}),
    ("B cell",     "GC B cell",                {"CD20": "pos", "BCL6": "pos", "Ki67": "pos"}),
    ("B cell",     "Mantle B cell",            {"CD20": "pos", "BCL6": "allneg"}),
    ("Immune",     "Plasma cell",              {"CD138": "pos"}),
    ("Structural", "FDC",                       {"CD21": "anypos", "CD35": "anypos"}),
    ("Immune",     "T cell",                   {"CD3e": "pos"}),
    ("T cell",     "CD4 T cell",               {"CD3e": "pos", "CD4": "pos", "CD8a": "allneg"}),
    ("T cell",     "CD8 T cell",               {"CD3e": "pos", "CD8a": "pos", "CD4": "allneg"}),
    ("T cell",     "Treg",                     {"CD3e": "pos", "CD4": "pos", "FOXP3": "pos"}),
    ("T cell",     "Tfh cell",                 {"CD3e": "pos", "CD4": "pos", "PD1": "pos"}),
    ("Immune",     "Macrophage",               {"CD68": "pos"}),
    ("Macrophage", "Tingible-body macrophage", {"CD68": "pos", "CD163": "pos"}),
    ("Immune",     "Dendritic cell",           {"CD11c": "pos", "HLADR": "pos"}),
    ("Immune",     "NK cell",                  {"CD56": "pos", "CD3e": "allneg"}),
    ("Immune",     "Neutrophil",               {"MPO": "pos"}),
    ("Structural", "Endothelial",              {"CD31": "anypos", "CD34": "anypos"}),
    ("Structural", "Lymphatic",                {"PDPN": "pos"}),
    ("Structural", "Smooth muscle",            {"SMA": "pos"}),
    ("Structural", "Stromal",                  {"Vimentin": "pos"}),
]


def write_phenotype_rules(path: Path) -> None:
    header = ["parent", "phenotype"] + RULE_MARKERS
    rows = [header]
    for parent, pheno, gates in RULES:
        row = [parent, pheno] + [gates.get(m, "") for m in RULE_MARKERS]
        rows.append(row)
    pd.DataFrame(rows).to_csv(path, header=False, index=False)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def square_px(cx_um: float, cy_um: float, side_um: float) -> list[list[float]]:
    """Axis-aligned square polygon (closed ring) in PIXEL coords."""
    h = (side_um / 2.0) / PIXEL_SIZE_UM
    cx, cy = cx_um / PIXEL_SIZE_UM, cy_um / PIXEL_SIZE_UM
    return [[cx - h, cy - h], [cx + h, cy - h], [cx + h, cy + h],
            [cx - h, cy + h], [cx - h, cy - h]]


def feature(coords, cls_name, obj_name):
    return {
        "type": "Feature",
        "properties": {
            "objectType": "annotation",
            "name": obj_name,
            "classification": {"name": cls_name},
        },
        "geometry": {"type": "Polygon", "coordinates": [coords]},
    }


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------
def build(out_dir: Path, seed: int) -> None:
    rng = np.random.default_rng(seed)
    cells_dir = out_dir / "cells"
    json_dir = out_dir / "json"
    for d in (out_dir, cells_dir, json_dir):
        d.mkdir(parents=True, exist_ok=True)

    targets_rows, markers_rows, comp_rows, lgals3_rows = [], [], [], []
    lookup_rows = []

    for donor in DONORS:
        case, status = donor["case"], donor["status"]
        gj_features = []
        # lay follicles out on a coarse grid across the tissue plane
        for n in range(1, N_FOLLICLES + 1):
            fkey = f"Follicle_{n}"
            region_core = f"{fkey}_core"
            region_band = f"{fkey}_band"

            diam = float(rng.uniform(80, 160))            # follicle diameter, um
            r = diam / 2.0
            core_area = math.pi * r * r                    # region_um2 (core) -> diameter
            band_r = r + 25.0
            band_area = math.pi * (band_r ** 2 - r ** 2)

            cx = 400.0 + ((n - 1) % 3) * 600.0 + rng.uniform(-40, 40)
            cy = 400.0 + ((n - 1) // 3) * 600.0 + rng.uniform(-40, 40)

            # ---- Targets (core + band) : region_um2 drives diameter; class=structure
            struct_area = float(rng.uniform(200, 2500))
            targets_rows.append(dict(**{"Case ID": case, "Donor Status": status},
                                     region=region_core, name=region_core,
                                     type="follicle_core", **{"class": "Lymphatic"},
                                     area_um2=struct_area, region_um2=core_area,
                                     area_density=struct_area / core_area,
                                     count=int(rng.integers(1, 6))))
            targets_rows.append(dict(**{"Case ID": case, "Donor Status": status},
                                     region=region_band, name=region_band,
                                     type="follicle_band", **{"class": "Lymphatic"},
                                     area_um2=struct_area * 0.6, region_um2=band_area,
                                     area_density=(struct_area * 0.6) / band_area,
                                     count=int(rng.integers(1, 6))))

            # ---- Markers (per region_type x marker)
            for region, rtype in ((region_core, "follicle_core"), (region_band, "follicle_band")):
                n_cells = int(rng.integers(30, 160))
                for m in MARKERS:
                    pos_frac = float(rng.uniform(0.02, 0.7))
                    markers_rows.append({
                        "Case ID": case, "Donor Status": status,
                        "region": region, "name": region, "region_type": rtype,
                        "marker": m, "n_cells": n_cells,
                        "pos_count": int(round(pos_frac * n_cells)),
                        "pos_frac": round(pos_frac, 4),
                    })
                # one LGALS3 row per region for the LGALS3 sheet
                lg = float(rng.uniform(0.05, 0.5))
                lgals3_rows.append({
                    "Case ID": case, "Donor Status": status,
                    "region": region, "name": region, "region_type": rtype,
                    "marker": "LGALS3", "n_cells": n_cells,
                    "pos_count": int(round(lg * n_cells)), "pos_frac": round(lg, 4),
                })

            # ---- Composition (per follicle core): defining-marker fractions
            cells_total = int(rng.integers(40, 200))
            fr = {d["marker"]: float(rng.uniform(0.05, 0.6)) for d in DEFINING}
            comp_rows.append({
                "Case ID": case, "Donor Status": status,
                "region": region_core, "name": region_core, "cells_total": cells_total,
                "Ins_single": round(fr["CD20"] * 0.7, 4),
                "Glu_single": round(fr["BCL6"] * 0.7, 4),
                "Stt_single": round(fr["CD21"] * 0.7, 4),
                "Multi_Pos": round(float(rng.uniform(0.02, 0.2)), 4),
                "Triple_Neg": round(float(rng.uniform(0.1, 0.4)), 4),
                "Ins_any": round(fr["CD20"], 4),
                "Glu_any": round(fr["BCL6"], 4),
                "Stt_any": round(fr["CD21"], 4),
            })

            # ---- Spatial lookup
            lookup_rows.append({
                "case_id": case, "follicle_key": fkey,
                "centroid_x_um": round(cx, 2), "centroid_y_um": round(cy, 2),
                "area_um2": round(core_area, 2),
            })

            # ---- GeoJSON features (pixel coords)
            gj_features.append(feature(square_px(cx, cy, diam), "Follicle", fkey))
            gj_features.append(feature(square_px(cx, cy, diam + 2 * EXPANSION_UM),
                                       "FollicleExpanded", f"{fkey}_exp20um"))

            # ---- Per-follicle single-cell CSV (drill-down)
            _write_cells_csv(cells_dir / f"{case}_{fkey}.csv", rng, cx, cy, r, EXPANSION_UM)

        # write one GeoJSON per case
        gj = {"type": "FeatureCollection", "features": gj_features}
        (json_dir / f"{case}.geojson").write_text(json.dumps(gj))

    # ---- Excel workbook (physical sheet + column names the loader expects)
    donor_meta = {d["case"]: d for d in DONORS}

    def add_demo(df):
        df["Age"] = df["Case ID"].map(lambda c: donor_meta[c]["age"])
        df["Gender"] = df["Case ID"].map(lambda c: donor_meta[c]["sex"])
        return df

    xls_path = out_dir / "master_results.xlsx"
    with pd.ExcelWriter(xls_path, engine="openpyxl") as w:
        add_demo(pd.DataFrame(targets_rows)).to_excel(w, sheet_name="Follicle_Targets", index=False)
        add_demo(pd.DataFrame(markers_rows)).to_excel(w, sheet_name="Follicle_Markers", index=False)
        add_demo(pd.DataFrame(comp_rows)).to_excel(w, sheet_name="Follicle_Composition", index=False)
        add_demo(pd.DataFrame(lgals3_rows)).to_excel(w, sheet_name="LGALS3", index=False)

    pd.DataFrame(lookup_rows).to_csv(out_dir / "follicle_spatial_lookup.csv", index=False)
    write_phenotype_rules(out_dir / "phenotype_rules.csv")

    n_cells_files = len(list(cells_dir.glob("*.csv")))
    print(f"[synthetic] wrote {xls_path}")
    print(f"[synthetic]   Follicle_Targets rows:     {len(targets_rows)}")
    print(f"[synthetic]   Follicle_Markers rows:     {len(markers_rows)}")
    print(f"[synthetic]   Follicle_Composition rows: {len(comp_rows)}")
    print(f"[synthetic] wrote {n_cells_files} per-follicle cell CSVs -> {cells_dir}")
    print(f"[synthetic] wrote {len(DONORS)} GeoJSONs -> {json_dir}")
    print(f"[synthetic] wrote follicle_spatial_lookup.csv ({len(lookup_rows)} follicles)")
    print(f"[synthetic] wrote phenotype_rules.csv ({len(RULES)} phenotypes)")


def _write_cells_csv(path: Path, rng, cx, cy, r, expansion) -> None:
    rows = []
    n_core = int(rng.integers(40, 90))
    n_peri = int(rng.integers(15, 45))
    core_phenos = ["GC B cell", "Mantle B cell", "B cell", "FDC", "Tfh cell",
                   "Tingible-body macrophage", "CD4 T cell"]
    peri_phenos = ["CD4 T cell", "CD8 T cell", "Macrophage", "Dendritic cell",
                   "Plasma cell", "Endothelial", "Stromal", "Lymphatic"]

    def emit(n, radius_lo, radius_hi, pool, region):
        for _ in range(n):
            ang = rng.uniform(0, 2 * math.pi)
            rad = rng.uniform(radius_lo, radius_hi)
            x = cx + rad * math.cos(ang)
            y = cy + rad * math.sin(ang)
            pheno = str(rng.choice(pool))
            expr = {m: round(float(rng.uniform(0, 1.5)), 3) for m in MARKERS}
            key = PHENO_MARKER.get(pheno)
            if key in expr:
                expr[key] = round(float(rng.uniform(2.0, 5.0)), 3)  # boost defining marker
            rows.append({
                "X_centroid": round(x, 2), "Y_centroid": round(y, 2),
                "phenotype": pheno, "cell_region": region,
                "Cell Area": round(float(rng.uniform(30, 120)), 2),
                "Nucleus Area": round(float(rng.uniform(10, 45)), 2),
                **expr,
            })

    emit(n_core, 0, r, core_phenos, "core")
    emit(n_peri, r, r + expansion, peri_phenos, "peri")
    pd.DataFrame(rows).to_csv(path, index=False)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate synthetic LymphVizkit follicle data")
    default_out = Path(__file__).resolve().parents[1] / "data" / "app_data"
    ap.add_argument("--out", type=Path, default=default_out, help="output data/app_data dir")
    ap.add_argument("--seed", type=int, default=7, help="RNG seed for reproducibility")
    args = ap.parse_args()
    build(args.out, args.seed)


if __name__ == "__main__":
    main()
